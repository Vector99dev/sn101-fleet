// pm2 ecosystem for the SN101 solver.
//
// Usage:
//   pm2 start /home/ubuntu/sn101-fleet/deploy/ecosystem.config.cjs
//   pm2 save
//   pm2 startup            # then run the printed sudo command once
//
// Reliability knobs explained:
//   autorestart       — pm2 restarts the process on any non-zero exit
//   max_restarts      — cap on consecutive failed restarts before pm2 gives up
//   min_uptime        — counted toward "successful start"; below this = failure
//   restart_delay     — wait between failed restarts (linear backoff via exp_backoff_restart_delay)
//   max_memory_restart — kill + restart if RSS exceeds this (sentence-transformers can leak slowly)
//   kill_timeout      — graceful shutdown window before SIGKILL

module.exports = {
  apps: [
    {
      name: "sn101-solver",
      script: "/home/ubuntu/sn101-fleet/deploy/start-solver.sh",
      interpreter: "bash",
      cwd: "/home/ubuntu/sn101-fleet",
      autorestart: true,
      watch: false,
      max_restarts: 50,
      min_uptime: "20s",
      restart_delay: 3000,
      exp_backoff_restart_delay: 200,
      max_memory_restart: "2G",
      kill_timeout: 10000,
      out_file: "/home/ubuntu/.pm2/logs/sn101-solver-out.log",
      error_file: "/home/ubuntu/.pm2/logs/sn101-solver-err.log",
      merge_logs: true,
      time: true,
      env: {
        NODE_ENV: "production",
      },
    },
  ],
};
