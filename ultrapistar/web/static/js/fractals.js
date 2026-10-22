/* FractalViz — WebGL audio-reactive fractal scenes for audio-only songs.
 *
 * Four shader styles in the HyperFractal vein (original implementations):
 *   Hyper Plasma   — warped-coordinate sine plasma, palette cycling
 *   Morph Julia    — orbit-trap Julia set, seed drifts with the music
 *   Apollonian Goo — sphere-inversion gasket: nested bubbling goo
 *   Flavor Ribbons — layered sine-flow ribbons with glow trails
 *
 * Styles rotate on a timer with a brief brightness dip at the swap.
 * Audio enters as smoothed uniforms (bass/mid/treble + pulse) so the
 * scenes breathe and morph with the song. render() returns false when
 * the context is lost — the caller falls back to the 2D scene.
 */
const FractalViz = (() => {
  "use strict";

  const VERT = `
attribute vec2 a_pos;
void main() { gl_Position = vec4(a_pos, 0.0, 1.0); }`;

  const HEAD = `
precision highp float;
uniform vec2 u_res;
uniform float u_time, u_bass, u_mid, u_tre, u_pulse, u_fade;
vec3 pal(float t) {
  return 0.5 + 0.5 * cos(6.2831 * (t + vec3(0.0, 0.33, 0.67)));
}
vec2 uv() {
  vec2 p = (gl_FragCoord.xy - 0.5 * u_res) / min(u_res.x, u_res.y);
  return p * 2.0;
}
vec3 dim(vec3 c) { return c * u_fade; }
`;

  const STYLES = [
  {
    name: "Hyper Plasma",
    frag: HEAD + `
void main() {
  vec2 p = uv();
  float t = u_time * 0.6;
  // warp the domain by the pulse — the plasma boils on the beat
  p += 0.35 * u_pulse * vec2(sin(t * 0.7 + p.y * 2.0),
                             cos(t * 0.5 + p.x * 2.0));
  float v = sin(p.x * 6.0 + t)
          + sin(p.y * 5.0 - t * 1.3)
          + sin((p.x + p.y) * 4.0 + t * 0.7)
          + sin(length(p) * (7.0 + u_mid * 6.0) - t * 2.0);
  v = v * 0.25 + 0.5;
  vec3 c = pal(v * 0.9 + u_time * 0.03 + u_bass * 0.15);
  // treble adds a fine shimmer layer on top
  c += pal(v * 3.0 - u_time * 0.05) * u_tre * 0.35;
  c *= 0.75 + u_pulse * 0.6;
  gl_FragColor = vec4(dim(c), 1.0);
}`},
  {
    name: "Morph Julia",
    frag: HEAD + `
void main() {
  vec2 p = uv() * (1.15 - u_pulse * 0.25);
  float t = u_time * 0.22;
  // the seed point wanders a slow Lissajous — the set breathes shape
  vec2 c = vec2(sin(t) * 0.75, cos(t * 0.83) * 0.62)
         + vec2(sin(t * 0.31), cos(t * 0.47)) * 0.1;
  c += vec2(u_bass - 0.5, u_mid - 0.5) * 0.06;
  vec2 z = p;
  float trap = 1e9, glow = 0.0;
  for (int i = 0; i < 14; i++) {
    z = vec2(z.x * z.x - z.y * z.y, 2.0 * z.x * z.y) + c;
    float m = dot(z, z);
    trap = min(trap, abs(m - 1.0));      // ring orbit-trap
    glow += exp(-m);
    if (m > 16.0) break;
  }
  float col = smoothstep(0.0, 1.4, trap) + glow * 0.12;
  vec3 cc = pal(col * 0.6 + u_time * 0.02 + u_tre * 0.1);
  cc *= 0.35 + min(glow, 1.0) * (0.8 + u_pulse * 0.8);
  cc *= 1.0 - 0.55 * smoothstep(0.9, 2.0, length(p));  // vignette the rim
  gl_FragColor = vec4(dim(cc), 1.0);
}`},
  {
    name: "Apollonian Goo",
    frag: HEAD + `
void main() {
  vec2 q = uv();
  float t = u_time * 0.35;
  // sphere-inversion gasket: fold space into nested bubbles
  vec3 p = vec3(q, 0.4 * sin(t * 0.4));
  p.xy += 0.2 * u_pulse * vec2(sin(t), cos(t * 1.3));
  float scale = 1.15 + u_mid * 0.35;
  float trap = 1e9;
  for (int i = 0; i < 9; i++) {
    p = -1.0 + 2.0 * fract(0.5 * p + 0.5);
    float r2 = dot(p, p);
    trap = min(trap, abs(r2 - 0.8));
    p *= scale / max(r2, 0.28);
  }
  float d = 0.5 * log(trap + 0.02);
  vec3 c = pal(d * 0.7 + u_time * 0.025 + u_bass * 0.2);
  c = c * (0.3 + 0.9 * smoothstep(0.0, 0.9, trap));
  // bass pulses push a hot rim into the cells
  c += pal(d + 0.5) * u_pulse * u_pulse * 0.8;
  gl_FragColor = vec4(dim(c), 1.0);
}`},
  {
    name: "Flavor Ribbons",
    frag: HEAD + `
void main() {
  vec2 p = uv();
  float t = u_time * 0.5;
  vec3 acc = vec3(0.0);
  for (int i = 0; i < 6; i++) {
    float k = float(i);
    float speed = 0.5 + k * 0.13;
    // each ribbon is a wavy filament; treble bends the wavefront
    float bend = (0.30 + u_tre * 0.35) *
                 sin(p.x * (2.2 + k * 0.6) + t * speed
                     + sin(p.x * 5.0 - t) * 0.5);
    float y = bend + (k - 2.5) * 0.26
              + 0.06 * sin(t * 0.9 + k * 2.1);
    float d = abs(p.y - y);
    float glowI = exp(-d * (14.0 - u_pulse * 6.0));
    float hue = k * 0.17 + u_time * 0.02 + u_bass * 0.1;
    acc += pal(hue) * glowI * (0.55 + u_pulse * 0.7);
  }
  // soft bloom floor so the gaps aren't black
  acc += pal(u_time * 0.015 + length(p) * 0.4) * 0.10;
  gl_FragColor = vec4(dim(acc), 1.0);
}`},
  ];

  const ROTATE_S = 34.0;      // seconds per style
  const DIP_S = 0.6;          // brightness dip at each swap

  let gl = null, canvas = null;
  let programs = [], cur = 0;
  let pulse = 0, styleT = 0, fade = 1;
  let scale = 1, fpsAcc = 0, fpsN = 0;

  function compile(src, type) {
    const s = gl.createShader(type);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
      console.warn("FractalViz shader:", gl.getShaderInfoLog(s));
      return null;
    }
    return s;
  }

  function build(style) {
    const vs = compile(VERT, gl.VERTEX_SHADER);
    const fs = compile(style.frag, gl.FRAGMENT_SHADER);
    if (!vs || !fs) return null;
    const p = gl.createProgram();
    gl.attachShader(p, vs);
    gl.attachShader(p, fs);
    gl.bindAttribLocation(p, 0, "a_pos");
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) return null;
    const u = {};
    for (const n of ["u_res", "u_time", "u_bass", "u_mid", "u_tre",
                     "u_pulse", "u_fade"])
      u[n] = gl.getUniformLocation(p, n);
    return { prog: p, u, name: style.name };
  }

  function init(cv) {
    canvas = cv;
    gl = cv.getContext("webgl",
                       { alpha: false, antialias: false, depth: false })
      || cv.getContext("experimental-webgl");
    if (!gl) return false;
    programs = STYLES.map(build).filter(Boolean);
    if (!programs.length) { gl = null; return false; }
    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER,
                  new Float32Array([-1, -1, 3, -1, -1, 3]),
                  gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    return true;
  }

  /* bands: {bass, mid, tre} 0..1 each; dt: seconds since last frame */
  function render(bands, dt) {
    if (!gl || !programs.length) return false;
    if (gl.isContextLost && gl.isContextLost()) return false;
    pulse += (bands.bass - pulse) * Math.min(1, dt * 9);
    // adaptive resolution: sustained low fps shrinks the drawing buffer
    // (software-GL fallback, weak GPU) — CSS keeps it fullscreen
    fpsAcc += dt; fpsN++;
    if (fpsAcc >= 2) {
      const fps = fpsN / fpsAcc;
      fpsAcc = 0; fpsN = 0;
      if (fps < 28 && scale > 0.5) {
        scale = Math.max(0.5, scale - 0.25);
        const r = canvas.getBoundingClientRect();
        const dpr = Math.min(window.devicePixelRatio || 1, 1.5) * scale;
        canvas.width = Math.max(2, Math.round(r.width * dpr));
        canvas.height = Math.max(2, Math.round(r.height * dpr));
      }
    }
    styleT += dt;
    if (styleT > ROTATE_S) { cur = (cur + 1) % programs.length;
                             styleT = 0; fade = 0; }
    fade = Math.min(1, fade + dt / DIP_S);
    const P = programs[cur];
    gl.useProgram(P.prog);
    gl.viewport(0, 0, canvas.width, canvas.height);
    gl.uniform2f(P.u.u_res, canvas.width, canvas.height);
    gl.uniform1f(P.u.u_time, performance.now() / 1000);
    gl.uniform1f(P.u.u_bass, bands.bass);
    gl.uniform1f(P.u.u_mid, bands.mid);
    gl.uniform1f(P.u.u_tre, bands.tre);
    gl.uniform1f(P.u.u_pulse, pulse);
    gl.uniform1f(P.u.u_fade, 0.25 + 0.75 * fade);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    return true;
  }

  function styleName() { return programs[cur] ? programs[cur].name : ""; }

  return { init, render, styleName, STYLES: STYLES.map(s => s.name) };
})();
