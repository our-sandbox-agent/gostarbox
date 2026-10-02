import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseHash, normalizePosts, hasPost, timestamp, fmtDate } from './blog-content.js';

const entry = (slug, frontmatter) => [`./content/blog/${slug}.mdx`, { frontmatter, default: () => null }];

test('hash parsing recognizes list and article forms', () => {
  assert.deepEqual(parseHash('#/blog'), { slug: null });
  assert.deepEqual(parseHash('#/blog/hello-sandbox'), { slug: 'hello-sandbox' });
});

test('hash parsing rejects empty, non-blog, nested, trailing-slash and wrong-case forms', () => {
  for (const bad of ['', '#', '#/', '#/files', '#/blog/', '#/blog/a/b', '#/blog/a/', '#/Blog', 'blog', '#/blogg', '#/BLOG/x', null, undefined, 42]) {
    assert.equal(parseHash(bad), null, `expected null for ${String(bad)}`);
  }
});

test('unknown slugs parse but do not resolve to a post', () => {
  const posts = normalizePosts({ './content/blog/a.mdx': { frontmatter: { title: 'A', date: '2026-01-01' } } });
  assert.deepEqual(parseHash('#/blog/no-such-post'), { slug: 'no-such-post' });
  assert.equal(hasPost(posts, 'no-such-post'), false);
  assert.equal(hasPost(posts, 'a'), true);
});

test('files without a frontmatter title are filtered out', () => {
  const posts = normalizePosts({
    './content/blog/kept.mdx': { frontmatter: { title: 'Kept', date: '2026-02-01' } },
    './content/blog/no-meta.mdx': {},
    './content/blog/empty-title.mdx': { frontmatter: { title: '', date: '2026-03-01' } },
    './content/blog/no-frontmatter.mdx': { default: () => null },
  });
  assert.deepEqual(posts.map(p => p.slug), ['kept']);
});

test('posts sort newest-first and keep input order for identical dates', () => {
  const posts = normalizePosts({
    './content/blog/old.mdx': { frontmatter: { title: 'Old', date: '2025-12-31' } },
    './content/blog/mid-b.mdx': { frontmatter: { title: 'Mid B', date: '2026-01-05' } },
    './content/blog/mid-a.mdx': { frontmatter: { title: 'Mid A', date: '2026-01-05' } },
    './content/blog/new.mdx': { frontmatter: { title: 'New', date: '2026-02-02' } },
  });
  assert.deepEqual(posts.map(p => p.slug), ['new', 'mid-b', 'mid-a', 'old']);
});

test('date strings and Date objects normalize to the same instant and zh-TW label', () => {
  assert.equal(timestamp('2026-01-05'), timestamp(new Date('2026-01-05T00:00:00Z')));
  assert.equal(fmtDate('2026-01-05'), '2026年1月5日');
  assert.equal(fmtDate(new Date('2026-01-05T00:00:00Z')), '2026年1月5日');
});

test('invalid and missing dates degrade to zero and an empty label without throwing', () => {
  assert.equal(timestamp(undefined), 0);
  assert.equal(timestamp('not-a-date'), 0);
  assert.equal(fmtDate('not-a-date'), '');
  const posts = normalizePosts({ './content/blog/x.mdx': { frontmatter: { title: 'X' } } });
  assert.equal(posts.length, 1);
});
