# Linux 服务器部署清单（Ubuntu / CentOS）

> 目标形态：子域名 HTTPS → Caddy → 后端 :8000（后端同时托管前端 dist）→ Qdrant :6333
> 全程约 30~40 分钟。每步都有验证命令，做完一步验一步。

## 0. 前置条件

- [ ] 域名已备案（✅ zhengyun.online 已备案，粤ICP备2026164325号；子域名 `zhitu.zhengyun.online` 无需单独备案），在阿里云 DNS 解析（当前截图所在页面）添加 A 记录：主机记录 `zhitu` → 记录值填服务器公网 IP（43.142.33.191）
- [ ] 腾讯云控制台 → 轻量/CVM **安全组**放行 80、443（**8000/6333 不要对公网开放**）
- [ ] Python 3.11+（`python3 --version`）

## 1. 上传代码

```bash
# 方式一：git（推荐）；方式二：打包上传（排除 node_modules/venv/data）
mkdir -p /opt/hr-ai
# rsync -avz --exclude node_modules --exclude venv --exclude data ./ user@server:/opt/hr-ai/
```

## 2. 后端

```bash
cd /opt/hr-ai/backend
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp ../deploy/env.production.example .env   # 然后逐项填写（见第 3 步）
```

## 3. 填写 .env（逐项核对）

- [ ] `JWT_SECRET_KEY`：`python3 -c "import secrets; print(secrets.token_urlsafe(48))"`
- [ ] `CORS_ORIGINS=https://zhitu.zhengyun.online`
- [ ] `ADMIN_PASSWORD`：改掉 admin123（首登后系统内再改一次）
- [ ] `DEFAULT_LLM_API_KEY` / `DEFAULT_EMBEDDING_API_KEY`
- [ ] `QDRANT_URL=http://127.0.0.1:6333`（不要写 localhost）
- [ ] SMTP：单人使用可先留空

## 4. Qdrant

```bash
# 不装 Docker 的方案：Linux 原生二进制
mkdir -p /opt/hr-ai/qdrant /opt/hr-ai/data/qdrant
curl -L https://github.com/qdrant/qdrant/releases/download/v1.19.2/qdrant-x86_64-unknown-linux-gnu.tar.gz | tar xz -C /opt/hr-ai/qdrant
cp /opt/hr-ai/qdrant-native/config/config.yaml /opt/hr-ai/qdrant/config.yaml
# 编辑 config.yaml：storage/snapshots 路径改为 /opt/hr-ai/data/qdrant
```

## 5. 前端构建

```bash
cd /opt/hr-ai/frontend
# 服务器上构建（需要 Node 20+）或本地构建后上传 dist/
npm ci && npm run build     # 产物在 frontend/dist，后端会自动托管
```

## 6. systemd 托管

```bash
sudo cp /opt/hr-ai/deploy/systemd/hr-ai-qdrant.service /etc/systemd/system/
sudo cp /opt/hr-ai/deploy/systemd/hr-ai-backend.service /etc/systemd/system/
# ⚠️ 编辑两份 unit 里的 User / 路径，与实际部署位置一致
sudo systemctl daemon-reload
sudo systemctl enable --now hr-ai-qdrant hr-ai-backend
```

验证：
```bash
curl -s http://127.0.0.1:6333 | head -c 120      # Qdrant 版本信息
curl -s http://127.0.0.1:8000/api/health          # {"status":"ok"}
curl -s http://127.0.0.1:8000/ | head -c 120      # 返回 index.html（前端托管生效）
```

## 7. nginx 反代 + HTTPS（⚠️ 本服务器实际方案：nginx 已占用 80/443，不要装 Caddy）

> 前提：服务器上 nginx 已在跑其他站点（如 www.zhengyun.online）。以下操作**只新增文件，不动现有站点**。

```bash
# 7.1 追加子域名配置（先只配 HTTP，供 certbot 验证）
sudo cp /opt/hr-ai/deploy/nginx/zhitu.conf /etc/nginx/sites-available/zhitu.conf
sudo ln -s /etc/nginx/sites-available/zhitu.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# 7.2 签发证书（certbot 会自动改写配置：加 443 server 块 + 80→443 跳转）
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d zhitu.zhengyun.online
```

验证：
```bash
curl -I http://zhitu.zhengyun.online            # 200 / 301 均正常（能到 nginx 就说明 server 块生效）
curl -s https://zhitu.zhengyun.online/api/health # {"status":"ok"}
```
浏览器打开 `https://zhitu.zhengyun.online` → 登录页正常、问答能流式输出。

## 8. 上线前最后检查

- [ ] 超管密码已改（系统内「用户管理」或重新初始化）
- [ ] `curl https://zhitu.zhengyun.online/api/openapi.json` —— 公网能看到接口文档属正常（有登录保护），介意可在 nginx 里拦 `/docs` `/redoc`
- [ ] **开通后 30 日内完成公安联网备案**（腾讯云备案控制台有「公安联网备案」入口，或到 beian.mps.gov.cn 提交）
- [ ] 免登录确认通道按需开（`DOC_CONFIRM_PUBLIC_ENABLED`，默认关）
- [ ] 定时备份：`crontab` 一条任务，打包 `backend/hr_ai.db + backend/uploads/ + data/qdrant/`
- [ ] 观察一天 `journalctl -u hr-ai-backend -f`

## 常见问题

| 症状 | 原因 |
|---|---|
| 回答卡住很久然后一次性出现 | 反代缓冲了 SSE：nginx 确认 `proxy_buffering off`（zhitu.conf 已带）；Caddy 用 `flush_interval -1` |
| 每个请求慢 2 秒 | Qdrant 地址写成了 `localhost`（解析成 IPv6），改 `127.0.0.1` |
| 首页白屏、控制台 404 | `dist` 没构建或后端找不到 `frontend/dist` 目录 |
| 登录后马上又要求登录 | 服务器与客户端时间不同步：`timedatectl set-ntp true` |
| 首次启动很慢（约 1 分钟） | 正常：在建向量集合并回灌已发布条目 |
