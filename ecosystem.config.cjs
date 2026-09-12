// PM2 config: pm2 start ecosystem.config.cjs
module.exports = {
  apps: [
    {
      name: "member-tracker-bot",
      script: "main.py",
      interpreter: "python3",
      cwd: __dirname,
      autorestart: true,
      max_restarts: 20,
      restart_delay: 5000,
      env: { PYTHONUNBUFFERED: "1" },
    },
  ],
};
