// cobol-xstate reference driver — executes an emitted machine end-to-end so the
// recovered contract can be golden-mastered against recorded COBOL I/O.
//
// WHY THIS EXISTS. The emitted XState machine is a *control-flow contract*: its
// `ops`/`guardFns` carry the decimal data semantics, but the effect actions
// (`perform_*`, `read_*`, OPEN/CLOSE/DISPLAY) are no-ops and PERFORM has no
// call-return (XState has no call stack — see README "Honest limitations"). So
// `createActor` alone cannot reproduce a batch run. This driver supplies exactly
// the missing pieces — PERFORM call-return and sequential file I/O — and nothing
// else: every data mutation still flows through the emitted `ops`, every branch
// through the emitted guards. If the driver reproduces the COBOL outputs, the
// recovered ops+guards+control-flow ARE the program.
//
// It is a deliberately small interpreter, faithful to the emitted shape:
//   * Paragraphs are top-level states; a paragraph's sub-states are `PARA__suffix`.
//   * `perform_P` runs paragraph P and RETURNS when control would fall through to a
//     different paragraph (COBOL PERFORM call-return). A `final` state reached at top
//     level ends the program; reached inside a PERFORM it is a return.
//   * `read_F` consumes the next record of file F into context; at end it sets the
//     external guard `F_atEnd` (COBOL leaves the record area unchanged at end).
//   * Transitions are `always` only (these batch contracts are autonomous). Guards
//     resolve against guardFns, then external flags, else false.
//
// Honest scope: GO TO into another paragraph is indistinguishable from fall-through
// once provenance `meta` is stripped, so this driver models the PERFORM/fall-through
// closed loop (the canonical batch program); it does not chase cross-paragraph GO TO
// or record-area→WORKING-STORAGE moves that the contract doesn't capture.

const paraOf = (key) => key.split('__')[0];

const HALT_ACTIONS = new Set(['STOP_RUN', 'STOPRUN', 'GOBACK', 'EXIT_PROGRAM']);

/**
 * Run an emitted machine module against recorded inputs.
 *
 * @param {object} mod  the emitted module: { machineConfig, ops, guardFns, externalGuards }
 * @param {object} opts
 *   files:     { [FILE]: Array<Record> }  records keyed by SELECT name (e.g. "CUST-FILE")
 *   guards:    { [name]: (context)=>bool } overrides for external/unknown guards
 *   maxSteps:  safety bound against a non-terminating contract
 * @returns {{ context, display, cycles, halted, steps }}
 *   context: final business context (no __cobol_external)
 *   display: array of DISPLAY outputs in order
 *   cycles:  context snapshot after each READ (the per-record-cycle trace)
 */
export function drive(mod, { files = {}, guards = {}, maxSteps = 1_000_000 } = {}) {
  const { machineConfig, ops, guardFns, externalGuards = [] } = mod;
  const states = machineConfig.states;
  const context = { ...machineConfig.context, __cobol_external: {} };
  const display = [];
  const cycles = [];
  const cursors = {};
  const extSet = new Set(externalGuards);
  let halted = false;
  let steps = 0;

  const snapshot = () => {
    const s = { ...context };
    delete s.__cobol_external;
    return s;
  };

  function evalGuard(name) {
    if (name == null) return true;
    if (Object.prototype.hasOwnProperty.call(guards, name)) return Boolean(guards[name](context));
    if (guardFns[name]) return Boolean(guardFns[name](context));
    if (extSet.has(name)) return Boolean(context.__cobol_external[name]);
    return false; // unknown guard: external, defaults false (COBOL hasn't set it)
  }

  function doOpen(action) {
    // OPEN_INPUT_FOO / OPEN_OUTPUT_FOO / OPEN_I-O_FOO / OPEN_FOO — file is the last segment.
    const file = action.split('_').pop();
    cursors[file] = 0;
    delete context.__cobol_external[file + '_atEnd'];
  }

  function doRead(file) {
    const recs = files[file] || [];
    const i = cursors[file] || 0;
    if (i < recs.length) {
      Object.assign(context, recs[i]);
      cursors[file] = i + 1;
    } else {
      context.__cobol_external[file + '_atEnd'] = true; // record area unchanged
    }
    cycles.push(snapshot());
  }

  function doDisplay(rest) {
    // operand is either a field name (resolve from context) or a literal (spaces→_).
    if (Object.prototype.hasOwnProperty.call(context, rest)) display.push(String(context[rest]));
    else display.push(rest.replace(/_/g, ' '));
  }

  function applyAction(name) {
    if (halted) return;
    if (ops[name]) { Object.assign(context, ops[name](context)); return; }
    if (HALT_ACTIONS.has(name)) { halted = true; return; }
    if (name.startsWith('perform_')) { perform(name.slice(8)); return; }
    if (name.startsWith('read_')) { doRead(name.slice(5)); return; }
    if (name.startsWith('OPEN_')) { doOpen(name); return; }
    if (name.startsWith('DISPLAY_')) { doDisplay(name.slice(8)); return; }
    // CLOSE_*, call_*, WRITE_*, and other effects are no-ops for a read-driven golden master.
  }

  // Run from `startKey`, owned by paragraph `owner`. isTop distinguishes the program's
  // main flow (a final state ends the run) from a PERFORMed body (a final / fall-through
  // to another paragraph is a return to the caller).
  function runFrom(startKey, owner, isTop) {
    let cur = startKey;
    for (;;) {
      if (halted) return { done: true };
      if (++steps > maxSteps) throw new Error('cobol-xstate driver: step limit exceeded (non-terminating contract?)');
      const st = states[cur];
      if (!st) throw new Error('cobol-xstate driver: unknown state ' + cur);

      for (const a of st.entry || []) {
        applyAction(a);
        if (halted) return { done: true };
      }

      if (st.type === 'final') return isTop ? { done: true } : { leave: true };

      let chosen;
      for (const t of st.always || []) {
        if (evalGuard(t.guard)) { chosen = t; break; }
      }
      if (!chosen) return { leave: true }; // nothing enabled: fall out of this region

      const target = chosen.target;
      const tgt = states[target];
      if (tgt && tgt.type === 'final') {
        if (isTop) return { done: true };
        return { leave: true }; // performed body fell off the end → return
      }
      if (paraOf(target) !== owner) return { leave: true }; // fall-through to next paragraph → return
      cur = target;
    }
  }

  function perform(para) {
    if (halted) return;
    const res = runFrom(para, para, false);
    if (res.done) halted = true; // STOP RUN / program end reached inside the performed body
  }

  const initial = machineConfig.initial;
  runFrom(initial, paraOf(initial), true);

  return { context: snapshot(), display, cycles, halted, steps };
}
