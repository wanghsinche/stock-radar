module.exports = {
  apps: [{
    name: 'stock-radar',
    script: 'uv',
    args: 'run python src/main.py',
    cwd: '/root/work/plusefin-landing-page/stock-radar',
    interpreter: 'none',
    instances: 1,
    autorestart: false,
    cron_restart: '0 6 * * 6',
    watch: false,
    max_memory_restart: '512M',
    log_file: '/root/work/plusefin-landing-page/stock-radar/logs/radar.log',
    out_file: '/root/work/plusefin-landing-page/stock-radar/logs/radar-out.log',
    error_file: '/root/work/plusefin-landing-page/stock-radar/logs/radar-error.log',
    log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
    merge_logs: true,
  }]
};
