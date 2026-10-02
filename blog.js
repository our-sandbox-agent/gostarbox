import { h, render as preactRender } from 'preact';
import { normalizePosts, blogList, blogPost, parseHash, hasPost as has } from './blog-content.js';

const glob = import.meta.glob('./content/blog/*.mdx', { eager: true });
const posts = normalizePosts(glob);

export const hasPost = slug => has(posts, slug);
export const blogListHTML = () => blogList(posts);
export const blogPostHTML = slug => blogPost(posts, slug);
export const parseBlogHash = (hash = location.hash) => parseHash(hash);

export function mountBlogBody(slug) {
  const el = document.querySelector('#blog-body');
  const p = posts.find(x => x.slug === slug);
  if (!el || !p) return;
  preactRender(h(p.Component, { components: {} }), el);
}
