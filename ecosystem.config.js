// pm2 进程配置：Web 服务 + 收盘后结算调度器（动量轮动）
const PY = "/usr/bin/python3";
const CWD = "/Users/visionclaw/momentum-sim";

module.exports = {
  apps: [
    {
      name: "momentum-web",
      script: "wsgi.py",
      interpreter: PY,
      cwd: CWD,
      env: { PORT: "8889", PYTHONWARNINGS: "ignore" },
      out_file: CWD + "/logs/web.out.log",
      error_file: CWD + "/logs/web.err.log",
      autorestart: true,
      max_restarts: 20,
    },
    {
      name: "momentum-scheduler",
      script: "scheduler.py",
      interpreter: PY,
      cwd: CWD,
      env: { PYTHONWARNINGS: "ignore", SIM_START: "2026-06-22" },
      out_file: CWD + "/logs/scheduler.out.log",
      error_file: CWD + "/logs/scheduler.err.log",
      autorestart: true,
      max_restarts: 20,
    },
  ],
};
