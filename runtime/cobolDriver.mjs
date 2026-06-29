// cobol-xstate reference driver — executes an emitted machine end-to-end so the
// recovered contract can be golden-mastered against recorded COBOL I/O.
//
// WHY THIS EXISTS. The emitted machine now models PERFORM as a real call-return
// (`invoke` of a per-paragraph actor, see emitter.py), so its control flow IS runnable
// under stock XState. What it still cannot express is sequential file I/O: `read_F` is a
// no-op and there is no record source. This driver runs the same config and supplies
// exactly that missing piece — feeding recorded records on READ, raising the AT-END
// external guard, and capturing DISPLAY — while every data mutation still flows through
// the emitted `ops` and every branch through the emitted guards. A match against the
// golden values is evidence the recovered ops+guards+control-flow reproduce the program.
//
// The interpreter is deliberately tiny because call-return is explicit in the config:
//   * Each state runs its `entry` actions, then either invokes (run the named actor to
//     its final, sharing context — equivalent to XState's input/output threading — and
//     continue at onDone.target), reaches a `final` (return / program end), or takes the
//     first enabled `always` transition.
//   * `read_F` consumes the next record of file F into context; at end it sets the
//     external guard `F_atEnd` (COBOL leaves the record area unchanged at end).
//
// Honest scope: records are supplied field-canonical (the driver does not re-quantize the
// record area on READ); GO TO into another paragraph is modeled as a return (the emitter
// cannot distinguish it from fall-through once provenance is stripped).

const HALT_ACTIONS = new Set(['STOP_RUN', 'STOPRUN', 'GOBACK', 'EXIT_PROGRAM']);

/**
 * Run an emitted machine module against recorded inputs.
 *
 * @param {object} mod  the emitted module: { machineConfig, actorConfigs, ops, guardFns, externalGuards }
 * @param {object} opts
 *   files:    { [FILE]: Array<Record> }  records keyed by SELECT name (e.g. "CUST-FILE")
 *   guards:   { [name]: (context)=>bool } overrides for external/unknown guards
 *   maxSteps: safety bound against a non-terminating contract
 * @returns {{ context, display, cycles, halted, steps }}
 *   context: final business context (no __cobol_external)
 *   display: array of DISPLAY outputs in order
 *   cycles:  context snapshot after each READ (the per-record-cycle trace)
 */
export function drive(mod, { files = {}, guards = {}, maxSteps = 1_000_000 } = {}) {
  const { machineConfig, actorConfigs = {}, ops, guardFns, externalGuards = [] } = mod;
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
    const file = action.split('_').pop(); // OPEN_INPUT_FOO / OPEN_FOO — file is last segment
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
    if (Object.prototype.hasOwnProperty.call(context, rest)) display.push(String(context[rest]));
    else display.push(rest.replace(/_/g, ' '));
  }

  function applyAction(name) {
    if (halted) return;
    if (ops[name]) { Object.assign(context, ops[name](context)); return; }
    if (HALT_ACTIONS.has(name)) { halted = true; return; }
    if (name.startsWith('read_')) { doRead(name.slice(5)); return; }
    if (name.startsWith('OPEN_')) { doOpen(name); return; }
    if (name.startsWith('DISPLAY_')) { doDisplay(name.slice(8)); return; }
    // CLOSE_*, call_*, WRITE_*, and other effects are no-ops for a read-driven golden master.
  }

  // Run a states map from `startKey` until a final state (a PERFORMed actor's __RET__,
  // or the program's end). Context is shared across scopes — the same net effect as
  // XState threading the actor's input/output, but without copying.
  function runScope(states, startKey) {
    let cur = startKey;
    for (;;) {
      if (halted) return;
      if (++steps > maxSteps) throw new Error('cobol-xstate driver: step limit exceeded (non-terminating contract?)');
      const st = states[cur];
      if (!st) throw new Error('cobol-xstate driver: unknown state ' + cur);

      for (const a of st.entry || []) {
        applyAction(a);
        if (halted) return;
      }

      if (st.type === 'final') return;

      if (st.invoke) {
        const sub = actorConfigs[st.invoke.src];
        if (!sub) throw new Error('cobol-xstate driver: unknown actor ' + st.invoke.src);
        runScope(sub.states, sub.initial);
        if (halted) return;
        cur = st.invoke.onDone.target;
        continue;
      }

      let chosen;
      for (const t of st.always || []) {
        if (evalGuard(t.guard)) { chosen = t; break; }
      }
      if (!chosen) return; // nothing enabled: fall out of this scope
      cur = chosen.target;
    }
  }

  // A parallel machine (DECLARATIVES/CICS HANDLE) runs its PROGRAM region; the orthogonal
  // HANDLERS region is reactive (its edges fire on runtime error events), so it idles here.
  if (machineConfig.type === 'parallel') {
    const prog = machineConfig.states.PROGRAM;
    runScope(prog.states, prog.initial);
  } else {
    runScope(machineConfig.states, machineConfig.initial);
  }
  return { context: snapshot(), display, cycles, halted, steps };
}
