# HTTPS for the control panel

By default the control panel runs over plain **HTTP** on port 8091. On a home network that only you reach, that is fine - the panel always needs the admin password, and change requests are checked for a custom header and same-origin, so another website can't drive it.

Turn on HTTPS if you reach the panel over the wider internet (through a VPN, a tunnel or a reverse proxy), or if your browser warns about sending the password in the clear. There are three ways; pick one.

## 1. Built-in HTTPS (self-signed, no extra software)

Set this in `config.env` and re-run the installer:

```
PANEL_HTTPS=yes
```

```
sudo bash install.sh 60-control-panel
```

The installer makes a self-signed certificate in `/opt/ai/secrets/panel-cert.pem` (key `panel-key.pem`, readable only by the panel user) and the panel then serves `https://SERVER-IP:8091`. Your browser warns once that the certificate is self-signed - that is expected; trust it and continue. To use your own certificate instead, drop it in those two files and restart: `sudo systemctl restart ai-control-panel`.

Under the hood the panel reads two environment variables, so you can also point it at any certificate without the installer:

```
# /etc/systemd/system/ai-control-panel.service  ->  [Service]
Environment=PANEL_TLS_CERT=/path/cert.pem PANEL_TLS_KEY=/path/key.pem
```

It requires TLS 1.2 or newer, sets the session cookie **Secure** and sends **HSTS** once HTTPS is on.

## 2. A reverse proxy (Caddy - easiest, real certificate)

If the panel has a name that resolves on the internet, [Caddy](https://caddyserver.com) gets a real (Let's Encrypt) certificate on its own. Leave the panel on HTTP and keep it to localhost (`--host 127.0.0.1` in the unit), then:

```
# /etc/caddy/Caddyfile
panel.example.com {
    reverse_proxy 127.0.0.1:8091 {
        header_up X-Forwarded-Proto https
    }
}
```

The panel trusts `X-Forwarded-Proto: https` **only from 127.0.0.1**, so a proxy running on the same machine switches on the Secure cookie and HSTS, while a stranger can't fake it from the network.

## 3. A reverse proxy (Nginx)

```
# /etc/nginx/sites-available/panel
server {
    listen 443 ssl;
    server_name panel.example.com;
    ssl_certificate     /etc/letsencrypt/live/panel.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/panel.example.com/privkey.pem;
    location / {
        proxy_pass http://127.0.0.1:8091;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
    }
}
```

Get the certificate with `certbot --nginx`.

## The chat site (Open WebUI) behind the same proxy

Open WebUI (port 3000) is a normal web app and sits behind the same proxy. With Caddy:

```
chat.example.com {
    reverse_proxy 127.0.0.1:3000
}
```

With Nginx, add a second `server { listen 443 ... location / { proxy_pass http://127.0.0.1:3000; } }` block, and for its live updates also pass the WebSocket headers:

```
proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection "upgrade";
```

Open WebUI reads the forwarded headers already (`ENABLE_FORWARD_USER_INFO_HEADERS=true` is set by the installer). Don't expose port 3000 or 8091 to the internet directly once a proxy handles them - close them in the firewall and let the proxy be the only way in.

---

Home AI Cluster · Made by [Shafakat Sayeedi](https://shafakat12.github.io/shafakat/)
