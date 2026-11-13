@echo off
cd /d "%~dp0"
echo ============================================
echo  Starting SQLBot (dataease/sqlbot) ...
echo ============================================
docker compose up -d
echo.
echo  SQLBot 启动中，稍后访问：
echo    Web 控制台 : http://localhost:8000   (admin / SQLBot@123456)
echo    MCP 服务   : http://localhost:8001/mcp
echo.
echo  查看日志：  docker compose logs -f sqlbot
echo  停止服务：  docker compose down
echo.
pause
