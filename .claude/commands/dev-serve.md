Start the Investment Analyst dev server and make it accessible to all devices on the local network.

Steps:
1. Check if something is already listening on port 8000. If so, report the URL and skip startup.
2. Verify `.env` has `HOST=0.0.0.0`. If it has `HOST=127.0.0.1`, update it to `HOST=0.0.0.0`.
3. Activate the virtualenv and start the server in the background:
   ```
   cd "/Users/zhichaozhong/Documents/Claude/Projects/Investment Analyst"
   source .venv/bin/activate && python -m server.app &
   ```
4. Wait up to 5 seconds for the server to come up, polling `/health` until it responds.
5. Get the local Wi-Fi IP with `ipconfig getifaddr en0` (fall back to `en1`).
6. Report:
   - Local URL: `http://127.0.0.1:8000`
   - Network URL: `http://<local-ip>:8000`
   - Provider and model from the `/health` response
   - Reminder: if other devices can't connect, check macOS Firewall (System Settings → Network → Firewall) allows python3 on port 8000
   - How to stop: `kill $(lsof -t -iTCP:8000)`
