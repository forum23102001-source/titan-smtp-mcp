# Titan SMTP connector on Render

1. Create a PRIVATE GitHub repo and upload these 4 files (server.py, requirements.txt, render.yaml, README.md).
2. Render dashboard -> New -> Blueprint -> pick the repo. Fill TITAN_USER and TITAN_PASS when asked. Deploy.
3. Render -> the service -> Environment -> copy MCP_SECRET.
4. Connector URL:  https://<your-service>.onrender.com/<MCP_SECRET>/mcp
5. claude.ai -> Settings -> Connectors -> Add custom connector -> paste the URL (no auth).

Treat the URL like a password: anyone who has it can send mail as you.
Free Render plans block SMTP ports, so use the Starter plan.
