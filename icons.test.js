import { test } from 'node:test';
import { readFileSync } from 'node:fs';

// Regression guard for the 2026-10-03 blank-page incident: the `const icons`
// map was dropped in a merge-conflict union while icon() kept referencing it,
// so the first render() threw ReferenceError and the deployed page rendered
// nothing — unit tests, build and curl-based smoke all stayed green. This test
// reads src.js as text (no DOM needed) and pins the two invariants curl
// cannot see: the map exists, and every name icon()/the nav passes resolves.
const src = readFileSync(new URL('./src.js', import.meta.url), 'utf8');

const map = src.match(/const icons=\{(.+?)\};/s);
test('icons map is defined in src.js', () => {
  if (!map) throw new Error('const icons={...} is missing from src.js (blank-page regression)');
});

const defined = new Set([...map[1].matchAll(/(?:^|,)(\w+):/g)].map(m => m[1]));
const literalUses = new Set([...src.matchAll(/icon\('(\w+)'\)/g)].map(m => m[1]));
// icon names passed through variables (nav triples, agent switch, etc.)
const dynamicUses = new Set(['grid', 'folder', 'chart', 'article', 'book']);

test('every literal icon() use resolves to a defined icon', () => {
  const missing = [...literalUses].filter(n => !defined.has(n));
  if (missing.length) throw new Error(`icon() references undefined icons: ${missing.join(', ')}`);
});

test('nav-driven icon names resolve to a defined icon', () => {
  const missing = [...dynamicUses].filter(n => !defined.has(n));
  if (missing.length) throw new Error(`nav icon names missing from the map: ${missing.join(', ')}`);
});
