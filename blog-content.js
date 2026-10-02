const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

export const timestamp = d => (typeof d === 'string' ? new Date(d + 'T00:00:00Z') : new Date(d)).getTime() || 0;

export const fmtDate = d => { const t = timestamp(d); return t ? new Date(t).toLocaleDateString('zh-TW', { year: 'numeric', month: 'long', day: 'numeric', timeZone: 'UTC' }) : ''; };

export const parseHash = hash => {
  const m = /^#\/blog(?:\/([^/]+))?$/.exec(hash);
  return m ? { slug: m[1] || null } : null;
};

export const normalizePosts = entries => Object.entries(entries)
  .map(([path, mod]) => ({ slug: path.slice(path.lastIndexOf('/') + 1).replace(/\.mdx$/, ''), meta: mod.frontmatter || {}, Component: mod.default }))
  .filter(p => p.meta.title)
  .sort((a, b) => timestamp(b.meta.date) - timestamp(a.meta.date));

export const hasPost = (posts, slug) => posts.some(p => p.slug === slug);

const tagsHTML = tags => Array.isArray(tags) && tags.length ? `<span class="blog-tags">${tags.map(t => `<i>${esc(t)}</i>`).join('')}</span>` : '';

export const blogList = posts => {
  return `<div class="page-title"><div><div class="eyebrow">NOTES FROM THE BUILD.</div><h1>Blog<span class="title-dot">.</span></h1><p>產品與工程決策的紀錄，包括失敗的驗證。</p></div></div>
<div class="blog-list">${posts.map(p => `<button class="blog-card" data-post="${esc(p.slug)}"><small>${esc(fmtDate(p.meta.date))}</small><strong>${esc(p.meta.title)}</strong>${p.meta.description ? `<span>${esc(p.meta.description)}</span>` : ''}${tagsHTML(p.meta.tags)}</button>`).join('') || '<p class="empty">還沒有文章。</p>'}</div>`;
};

export const blogPost = (posts, slug) => {
  const p = posts.find(x => x.slug === slug);
  if (!p) return blogList(posts);
  return `<section class="blog-post"><button class="subtle blog-back" data-back>← 返回文章列表</button><div class="blog-post-meta"><small>${esc(fmtDate(p.meta.date))}</small>${tagsHTML(p.meta.tags)}</div><h1 class="blog-title">${esc(p.meta.title)}</h1>${p.meta.description ? `<p class="blog-description">${esc(p.meta.description)}</p>` : ''}<div id="blog-body" class="blog-body"></div></section>`;
};
