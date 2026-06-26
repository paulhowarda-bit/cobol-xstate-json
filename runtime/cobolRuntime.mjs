// cobol-xstate runtime — fixed-point DECIMAL semantics for emitted XState v5 machines.
//
// COBOL arithmetic is fixed-point decimal, NEVER binary float. A numeric value is an
// integer coefficient scaled by a power of ten:  value = coef / 10**scale.  This module
// implements that, plus the field-aware *store* (truncate/round to a receiver's PICTURE)
// and the relational/class/sign tests the emitted guards reference. It has no
// dependencies so it can be unit-tested on its own.
//
// Honest simplifications (documented, not hidden):
//   * Division uses a fixed intermediate guard scale (DIV_GUARD) then quantizes to the
//     receiver — COBOL's exact "composite of operands" intermediate-precision rules are
//     not reproduced. For receiver scales <= DIV_GUARD this matches in practice.
//   * Storing into an unsigned field stores the magnitude; into a too-small integer part
//     it truncates high-order digits (COBOL default, i.e. no ON SIZE ERROR phrase).
//   * Figurative SPACE/SPACES is treated as "" and relies on space-padded comparison;
//     HIGH-VALUE/LOW-VALUE in arithmetic and uninterpretable forms call notModeled().

const DIV_GUARD = 18; // fractional guard digits kept through a division before quantize

function tenPow(n) {
  return 10n ** BigInt(n);
}

export class Dec {
  constructor(coef, scale) {
    this.coef = coef;      // BigInt
    this.scale = scale;    // int >= 0 (fractional digit count)
  }

  static fromString(s) {
    s = String(s).trim();
    let neg = false;
    if (s[0] === '+' || s[0] === '-') {
      neg = s[0] === '-';
      s = s.slice(1);
    }
    const dot = s.indexOf('.');
    let scale = 0;
    if (dot >= 0) {
      scale = s.length - dot - 1;
      s = s.slice(0, dot) + s.slice(dot + 1);
    }
    if (s === '') s = '0';
    let coef = BigInt(s);
    if (neg) coef = -coef;
    return new Dec(coef, scale);
  }

  // Re-express at a target scale (>= for padding; < truncates toward zero by default).
  rescale(scale, rounded = false) {
    if (scale === this.scale) return new Dec(this.coef, scale);
    if (scale > this.scale) {
      return new Dec(this.coef * tenPow(scale - this.scale), scale);
    }
    const drop = this.scale - scale;
    const factor = tenPow(drop);
    let q = this.coef / factor; // BigInt division truncates toward zero
    if (rounded) {
      const rem = this.coef % factor;
      const absRem = rem < 0n ? -rem : rem;
      if (absRem * 2n >= factor) q += this.coef < 0n ? -1n : 1n; // round half away from zero
    }
    return new Dec(q, scale);
  }

  neg() {
    return new Dec(-this.coef, this.scale);
  }

  toString() {
    const neg = this.coef < 0n;
    let digits = (neg ? -this.coef : this.coef).toString();
    if (this.scale === 0) return (neg ? '-' : '') + digits;
    if (digits.length <= this.scale) {
      digits = '0'.repeat(this.scale - digits.length + 1) + digits;
    }
    const intPart = digits.slice(0, digits.length - this.scale);
    const fracPart = digits.slice(digits.length - this.scale);
    return (neg ? '-' : '') + intPart + '.' + fracPart;
  }

  isZero() {
    return this.coef === 0n;
  }
}

function align(a, b) {
  const scale = Math.max(a.scale, b.scale);
  return [a.rescale(scale), b.rescale(scale), scale];
}

// ---- coercion ------------------------------------------------------------ //

// Coerce a context value / literal / Dec into a Dec.
export function D(x) {
  if (x instanceof Dec) return x;
  if (typeof x === 'bigint') return new Dec(x, 0);
  return Dec.fromString(x);
}

// ---- arithmetic (the COMPUTE / ADD / SUBTRACT / ... building blocks) ----- //

export function add(a, b) {
  const [x, y, scale] = align(D(a), D(b));
  return new Dec(x.coef + y.coef, scale);
}

export function sub(a, b) {
  const [x, y, scale] = align(D(a), D(b));
  return new Dec(x.coef - y.coef, scale);
}

export function mul(a, b) {
  const x = D(a), y = D(b);
  return new Dec(x.coef * y.coef, x.scale + y.scale);
}

export function div(a, b) {
  const x = D(a), y = D(b);
  if (y.coef === 0n) throw new Error('cobol-xstate: divide by zero');
  // a/b at DIV_GUARD fractional digits:
  //   (x.coef * 10**y.scale * 10**DIV_GUARD) / (y.coef * 10**x.scale)
  const num = x.coef * tenPow(y.scale + DIV_GUARD);
  const den = y.coef * tenPow(x.scale);
  return new Dec(num / den, DIV_GUARD);
}

export function pow(a, b) {
  const base = D(a), exp = D(b);
  if (exp.scale !== 0) notModeled('non-integer exponent in **');
  let n = exp.coef;
  if (n < 0n) notModeled('negative exponent in **');
  let result = new Dec(1n, 0);
  for (let i = 0n; i < n; i++) result = mul(result, base);
  return result;
}

// ---- store into a receiving field ---------------------------------------- //

// Quantize `value` (a Dec) to a numeric field spec and return its decimal STRING (kept
// JSON-serializable for XState context). spec = {digits, scale, signed, rounded}.
export function store(value, spec) {
  let d = D(value).rescale(spec.scale || 0, !!spec.rounded);
  if (!spec.signed && d.coef < 0n) d = new Dec(-d.coef, d.scale); // store magnitude
  // Truncate the integer part to (digits - scale) positions (COBOL default truncation).
  const intDigits = Math.max(0, (spec.digits || 0) - (spec.scale || 0));
  if (intDigits > 0) {
    const mod = tenPow(intDigits + (spec.scale || 0));
    let c = d.coef % mod;
    d = new Dec(c, d.scale);
  }
  return d.toString();
}

// Store into an alphanumeric field: left-justify, space-pad / truncate to `len`.
export function storeStr(value, spec) {
  let s = value == null ? '' : String(value);
  const len = spec && spec.len;
  if (len && len > 0) {
    s = s.length >= len ? s.slice(0, len) : s + ' '.repeat(len - s.length);
  }
  return s;
}

// ---- comparisons --------------------------------------------------------- //

function cmpDec(a, b) {
  const [x, y] = align(D(a), D(b));
  return x.coef < y.coef ? -1 : x.coef > y.coef ? 1 : 0;
}

function pad(s, n) {
  s = s == null ? '' : String(s);
  return s.length >= n ? s : s + ' '.repeat(n - s.length);
}

function cmpStr(a, b) {
  const n = Math.max(String(a ?? '').length, String(b ?? '').length);
  const x = pad(a, n), y = pad(b, n);
  return x < y ? -1 : x > y ? 1 : 0;
}

const REL = {
  '=': (c) => c === 0,
  '<>': (c) => c !== 0,
  '>': (c) => c > 0,
  '<': (c) => c < 0,
  '>=': (c) => c >= 0,
  '<=': (c) => c <= 0,
};

// numeric=true -> decimal compare; else COBOL alphanumeric (space-padded) compare.
export function rel(a, op, b, numeric) {
  const test = REL[op];
  if (!test) notModeled('relational operator ' + op);
  return test(numeric ? cmpDec(a, b) : cmpStr(a, b));
}

// ---- class / sign tests -------------------------------------------------- //

export function isClass(value, cls) {
  const s = value == null ? '' : String(value);
  switch (cls) {
    case 'NUMERIC':
      return /^[+-]?\d+$/.test(s.trim()) || /^\d*$/.test(s);
    case 'ALPHABETIC':
      return /^[A-Za-z ]*$/.test(s);
    case 'ALPHABETIC-UPPER':
      return /^[A-Z ]*$/.test(s);
    case 'ALPHABETIC-LOWER':
      return /^[a-z ]*$/.test(s);
    default:
      return notModeled('class ' + cls);
  }
}

export function isSign(value, sign) {
  const c = cmpDec(value, new Dec(0n, 0));
  if (sign === 'POSITIVE') return c > 0;
  if (sign === 'NEGATIVE') return c < 0;
  if (sign === 'ZERO') return c === 0;
  return notModeled('sign ' + sign);
}

// ---- honesty backstop ---------------------------------------------------- //

export function notModeled(what) {
  throw new Error('cobol-xstate: unmodeled construct — ' + what +
    ' (the COBOL→XState contract flagged this; supply a faithful implementation).');
}
