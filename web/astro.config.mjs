import { defineConfig } from 'astro/config';
import sitemap from '@astrojs/sitemap';

export default defineConfig({
  site: 'https://machine-account.pages.dev',
  integrations: [sitemap()],
  output: 'static'
});
