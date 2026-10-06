#!/bin/bash
# GPU Monitor 开发辅助脚本:后台启停 + 日志跟随,方便改代码后快速验证
# 用法: ./dev.sh start | stop | status | log
cd "$(dirname "$0")"
LOG=/tmp/gpu_monitor_dev.log

case "$1" in
  start)
    if pgrep -f "venv/bin/python app.py" > /dev/null; then
      echo "发现旧实例,先停止..."; pkill -f "venv/bin/python app.py"; sleep 1
    fi
    nohup ./venv/bin/python app.py > "$LOG" 2>&1 &
    sleep 2
    if pgrep -f "venv/bin/python app.py" > /dev/null; then
      echo "已启动 (PID $(pgrep -f 'venv/bin/python app.py'))"
      echo "菜单栏应已出现图标;看日志: ./dev.sh log;验证接口: ./dev.sh status"
    else
      echo "启动失败,日志如下:"; tail -20 "$LOG"
    fi
    ;;
  stop)
    pkill -f "venv/bin/python app.py" && echo "已停止" || echo "没有运行中的实例"
    ;;
  status)
    pgrep -f "venv/bin/python app.py" > /dev/null && echo "进程: 运行中" || echo "进程: 未运行"
    echo "Web:  $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18888/)"
    ;;
  log)
    tail -f "$LOG"
    ;;
  *)
    echo "用法: ./dev.sh start | stop | status | log"
    ;;
esac
