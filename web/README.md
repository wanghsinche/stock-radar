# 机器账户静态站

纯静态 Astro 站点，不使用 React，不连接 Webull，不提供 API。

## Cloudflare Pages 配置

- Root directory: `web`
- Build command: `npm run build`
- Build output directory: `dist`
- Node.js version: `20` 或更高

## 本地运行

```bash
cd web
npm install
npm run dev
```

## 数据更新

每周六/周一更新这些文件后提交 Git，Cloudflare Pages 会自动重新构建：

- `src/data/latest.json`
- `src/data/holdings.json`
- `src/data/trades.json`
- `src/data/weekly.json`
- `src/data/proofs.json`

公开截图放在 `public/proofs/`。原图建议只保存在仓库外或 `proofs/raw/`，不要提交。

## 推荐导出流程

周六公布策略，只读取本地策略 JSON，不调用 Webull：

```bash
uv run python -m src.site_export strategy
```

周一执行后同步真实账户、持仓和订单历史：

```bash
uv run python -m src.site_export live --config config.yml
```

默认不展示累计收益率，因为需要准确的净入金基准。如果要展示，可显式传入：

```bash
uv run python -m src.site_export live --config config.yml --initial-capital 22803.36
```

如果生产环境使用 `config.yaml`，可以省略 `--config`。

## 只发布网站文件

从仓库根目录运行：

```bash
bash scripts/publish_site.sh
```

可自定义 commit message：

```bash
bash scripts/publish_site.sh "chore: update machine account site"
```

脚本只会 stage 这些公开网站路径：

- `web/package.json`
- `web/package-lock.json`
- `web/astro.config.mjs`
- `web/tsconfig.json`
- `web/README.md`
- `web/public`
- `web/src`

不会 stage `config.yml`、`config.yaml`、`conf/token.txt`、`data/`、`proofs/raw/` 或其它本地文件。
