# Family & other devices

## Accounts for everyone

In Open WebUI (as the first/admin account): **Admin Panel → Users → ➕** for each person, role **user**. Everyone logs in at `http://SERVER-IP:3000` and sees only their own chats.

Phones and tablets: open the address in the browser and use **Add to Home screen**. It then works like an app.

## Let them use the models

New models are visible only to admins. For each model: **Admin Panel → Settings → Models → ✏️ → Access → Public**.
Features (web search, image generation, ...): **Admin Panel → Users → Groups → Default permissions → Features**.

For children you can create a "family" model in **Workspace → Models** on top of a base model, with a system prompt, and make only that one public. You can also use groups to give different people different models.

## Web search in chat

The installer presets web search for new installs. If it is off: **Admin Panel → Settings → Web Search** → on, engine **searxng**, URL `http://127.0.0.1:8888/search?q=<query>`. In a chat, turn it on with ➕ → Web Search.

## The control panel from any device

`http://SERVER-IP:8091` works from every device on the home network once you log in with the admin password. Settings shows which devices are logged in and can log them all out. Five wrong passwords lock that device out for 10 minutes.

## Coding tools and direct API access

The raw API (port 8080) has **no password**, so only allowed devices may use it:
* `ADMIN_PC_IP` from `config.env`, and
* the devices listed in **control panel → Settings → Use the AI from other devices**.

Then point the tool at the server:

| Tool | Setting |
|---|---|
| VS Code **Cline** / **Continue**, other OpenAI-compatible tools | Base URL `http://SERVER-IP:8080/v1`, API key `local` (anything), model e.g. `gpt-oss-20b` |
| **Claude Code** | `ANTHROPIC_BASE_URL=http://SERVER-IP:8080`, `ANTHROPIC_AUTH_TOKEN=local`, `ANTHROPIC_MODEL=gpt-oss-20b` |
| Browser AI extensions (e.g. Page Assist) | OpenAI-compatible, `http://SERVER-IP:8080/v1` |

On Windows (PowerShell: `Unblock-File .\setup-vscode.ps1; Set-ExecutionPolicy -Scope Process Bypass`), `clients/windows/setup-vscode.ps1 -Server SERVER-IP` creates a `claude-local` command, installs Cline and adds a desktop shortcut to the chat.
