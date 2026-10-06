import { defineConfig } from 'vite';
import mdx from '@mdx-js/rollup';
import remarkFrontmatter from 'remark-frontmatter';
import remarkMdxFrontmatter from 'remark-mdx-frontmatter';

export default defineConfig({
  base: '/gostarbox/',
  // Development only: keep browser API requests same-origin; Pages is demo-only.
  server: { proxy: { '/v1': 'http://127.0.0.1:8787' } },
  plugins: [
    mdx({
      jsxImportSource: 'preact',
      remarkPlugins: [remarkFrontmatter, remarkMdxFrontmatter],
    }),
  ],
});
