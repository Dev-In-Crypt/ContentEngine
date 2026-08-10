// Runs before first paint, which is the whole job: it sets data-theme so the
// dark layout never flashes light. A classic <script src> in <head> blocks the
// parser, so being early here is what guarantees it — and being ABOVE the
// vendored Tailwind makes this strictly faster than the inline version was,
// because that one sat below 407 KB of blocking script.
try{var _t=localStorage.getItem('theme')||'light';var _th=_t==='dark'?'dark':'light';document.documentElement.setAttribute('data-theme',_th);/* paint the themed bg immediately so the dark body classes don't flash on light */document.documentElement.style.backgroundColor='var(--bg)';}catch(e){}
