module.exports = {
  apps: [
    {
      name: "hydra-agent",
      script: "venv/bin/python3",
      args: "app.py",
      cwd: __dirname,
      interpreter: "none",
      // Restart policy — exponential backoff prevents crash loops
      autorestart: true,
      max_restarts: 10,
      min_uptime: "10s",
      restart_delay: 3000,
      exp_backoff_restart_delay: 1000,
      // Logging
      out_file: "./logs/hydra-agent-out.log",
      error_file: "./logs/hydra-agent-error.log",
      log_date_format: "YYYY-MM-DD HH:mm:ss Z",
      merge_logs: true,
      // Environment
      env: {
        PYTHONUNBUFFERED: "1",
      },
    },
  ],
};
