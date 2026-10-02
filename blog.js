import { h, render as preactRender } from 'preact';

const glob = import.meta.glob('./content/blog/*.mdx', { eager: true });
const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const timestamp = d => (typeof d === 'string' ? new Date(d + 'T00:00:00Z') : new Date(d)).getTime() || 0;
const fmtDate = d => { const t = timestamp(d); return t ? new Date(t).toLocaleDateString('zh-TW', { year: 'numeric', month: 'long', day: 'numeric', timeZone: 'UTC' }) : ''; };

const posts = Object.entries(glob)
  .map(([path, mod]) => ({ slug: path.slice(path.lastIndexOf('/') + 1).replace(/\.mdx$/, ''), meta: mod.frontmatter || {}, Component: mod.default }))
  .filter(p => p.meta.title)
  .sort((a, b) => timestamp(b.meta.date) - timestamp(a.meta.date));

export const hasPost = slug => posts.some(p => p.slug === slug);

const tagsHTML = tags => Array.isArray(tags) && tags.length ? `<span class="blog-tags">${tags.map(t => `<i>${esc(t)}</i>`).join('')}</span>` : '';

export function blogListHTML() {
  return `<div class="page-title"><div><div class="eyebrow">NOTES FROM THE BUILD.</div><h1>Blog<span class="title-dot">.</span></h1><p>產品與工程決策的紀錄，包括失敗的驗證。</p></div></div>
<div class="blog-list">${posts.map(p => `<button class="blog-card" data-post="${esc(p.slug)}"><small>${esc(fmtDate(p.meta.date))}</small><strong>${esc(p.meta.title)}</strong>${p.meta.description ? `<span>${esc(p.meta.description)}</span>` : ''}${tagsHTML(p.meta.tags)}</button>`).join('') || '<p class="empty">還沒有文章。</p>'}</div>`;
}

export function blogPostHTML(slug) {
  const p = posts.find(x => x.slug === slug);
  if (!p) return blogListHTML();
  return `<section class="blog-post"><button class="subtle blog-back" data-back>← 返回文章列表</button><div class="blog-post-meta"><small>${esc(fmtDate(p.meta.date))}</small>${tagsHTML(p.meta.tags)}</div><h1 class="blog-title">${esc(p.meta.title)}</h1>${p.meta.description ? `<p class="blog-description">${esc(p.meta.description)}</p>` : ''}<div id="blog-body" class="blog-body"></div></section>`;
}

export function mountBlogBody(slug) {
  const el = document.querySelector('#blog-body');
  const p = posts.find(x => x.slug === slug);
  if (!el || !p) return;
  preactRender(h(p.Component, { components: {} }), el);
}

export function parseBlogHash(hash = location.hash) {
  const m = /^#\/blog(?:\/([^/]+))?$/.exec(hash);
  return m ? { slug: m[1] || null } : null;
}
