# DS918+ 部署

适用：DSM 7.3.1、Intel AMD64；目录 `/volume2/docker/crowdroom`；现有 nginx 位于 `192.168.1.123`，入口为 `https://music.kylermaker.com`。此目录的 Compose 独立运行，不需要仓库源码或根目录的 override，也不使用上游 `latest` 镜像。

## 导入镜像与配置

将本目录三个文件及从当前源码构建的 **linux/amd64** 镜像包复制到 `/volume2/docker/crowdroom`。在 NAS SSH 中执行（Docker 命令需有相应权限）：

```sh
cd /volume2/docker/crowdroom
docker load -i crowdroom-dsm-20261026-amd64.tar.gz
docker image inspect crowdroom:dsm-20261026 --format '{{.Os}}/{{.Architecture}}'
cp .env.example .env
chmod 600 .env
```

镜像架构应为 `linux/amd64`。编辑 `.env`；全新安装分别用 `openssl rand -hex 32` 生成签名密钥和数据库密码，再生成加密密钥：

```sh
docker run --rm --entrypoint python crowdroom:dsm-20261026 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

迁移现有安装时沿用原 `SECRET_KEY`、`ENCRYPTION_KEY` 和 Spotify 凭据。丢失加密密钥会导致旧 Spotify 凭据无法解密。`.env`、数据库备份、头像和镜像包留在部署端；GitHub 只上传模板文件。镜像内不应包含 `.env` 或运行数据。

## 全新数据库

```sh
docker compose -p crowdroom up -d --wait db
docker compose -p crowdroom run --rm --no-deps --entrypoint alembic app upgrade head
docker compose -p crowdroom up -d app
docker compose -p crowdroom ps
```

如使用 Container Manager 的“项目”，项目名称固定为 `crowdroom`，路径选择本目录。首次迁移完成后再启动整个项目。数据库和头像分别保存在 `crowdroom_pgdata`、`crowdroom_avatars`；以后保持相同项目名。不要执行 `down -v`。

## 搬迁现有数据

在**旧部署目录、原 Compose 项目**中停止 app 写入并导出：

```sh
docker compose stop app
umask 077
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > crowdroom.dump
docker compose run --rm --no-deps --entrypoint tar app -C /app/backend/static/avatars -czf - . > avatars.tar.gz
```

将两个备份和原密钥安全移到 NAS，填写 NAS `.env`。以下恢复步骤仅用于**尚未初始化应用表的空目标数据库**；已有 NAS 数据时先备份，不要直接覆盖。不要先运行上一节的全新数据库迁移。

```sh
cd /volume2/docker/crowdroom
docker compose -p crowdroom up -d --wait db
docker compose -p crowdroom exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-acl --exit-on-error' < crowdroom.dump
docker compose -p crowdroom run --rm --no-deps --user 0 -v "$PWD:/backup:ro" --entrypoint sh app -c 'tar -xzf /backup/avatars.tar.gz -C /app/backend/static/avatars && chown -R app:app /app/backend/static/avatars'
docker compose -p crowdroom run --rm --no-deps --entrypoint alembic app current
docker compose -p crowdroom run --rm --no-deps --entrypoint alembic app upgrade head
docker compose -p crowdroom up -d app
docker compose -p crowdroom ps
```

等 db 显示 healthy 后再执行恢复或迁移。若旧库没有 Alembic 版本，先核对实际表结构与迁移基线，不要直接 `stamp head`。迁移失败时保持 app 停止；确认新站正常后再处理旧部署和备份。

## nginx、域名与验证

在现有 nginx 新增 `music.kylermaker.com` 主机，转发到 **NAS 内网 IP:3323**（HTTP）。沿用已有 443 转发和 DDNS-Go；Cloudflare 橙云记录应跟随现有公网入口，SSL/TLS 使用 Full (strict)，nginx 安装覆盖该域名的有效证书。网站、API、`/ws/` 全部走同一主机。

启用 WebSocket 转发：上游使用 HTTP/1.1，传递 `Upgrade` 和 `Connection` 头；读取超时设为至少 90 秒。nginx 应校验 Cloudflare 来源后解析真实访客 IP，再覆盖发往应用的 `X-Forwarded-For`；同时传递 `Host` 和 `X-Forwarded-Proto: https`。`.env` 的 `FORWARDED_ALLOW_IPS` 只信任应用实际看到的 nginx peer（预填 `192.168.1.123`，需要核实），不能填 `*`。NAS 的 3323 端口仅允许现有 nginx 和必要管理设备访问。

在 Spotify 开发者后台登记 `.env` 中的完整 HTTPS callback。使用手机移动网络测试：打开域名、登录、进入房间、跨设备实时更新队列、完成 Spotify 授权，并确认 NAS 重启后容器恢复。NAS 仅运行点歌服务；iPhone Spotify 设备休眠和 VoiceRoom 音频输入仍由实际播放设备决定。
