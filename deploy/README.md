# 部署说明（用户层，无需 root）

整套服务全部落在运行者的 `$HOME` 下，systemd 使用**用户单元**，反向代理是可选项。

## 一条命令安装

```bash
git clone <repo> ~/<slug> && cd ~/<slug>
sh deploy/install.sh            # 需要 Python 3.11+ 与 systemd
sh deploy/setup-node.sh         # 只有要用智能体时才需要（安装用户级 Node 与 pi）
```

安装脚本会：建目录 → 拉代码 → 生成 `~/.config/systemd/user/<slug>.service` →
`systemctl --user enable --now` → 健康检查。**不需要 root。**

想改路径/端口/名称/模型：写一份 `deploy/config.local.sh`（已在 `.gitignore` 中）覆盖
`deploy/config.sh` 里的默认值，例如：

```sh
APP_SLUG=my-health-kb
APP_PORT=9000
LLM_BASE_URL=http://127.0.0.1:8080/v1
LLM_MODEL=local-model
```

`sh deploy/install.sh --print` 可以先看一遍将使用的配置。

## 目录结构

| 位置 | 内容 |
|---|---|
| `$HOME/<slug>` | 系统代码（git 工作副本） |
| `$HOME/kb` | 数据仓库（私人档案，另一个仓库） |
| `$HOME/data` | SQLite、上传附件、`llm_api_key`（600，不进 git） |
| `$HOME/agent` | 智能体工作目录（只放净化后的 `CLAUDE.md`） |
| `$HOME/.config/<slug>/agent.env` | 智能体令牌与后端地址（600） |
| `$HOME/.config/systemd/user/` | 用户级服务与定时器 |
| `$HOME/.local/opt/node-v22/` | 用户级 Node（智能体用） |
| `$HOME/.local/bin/pi` | pi 包装脚本 |
| `$HOME/.local/state/<slug>/` | 日志、智能体状态（模型清单） |

## 让服务在退出登录后继续运行

用户级服务默认随用户会话存在。要在无人登录时也常驻，需要一次性执行（这条需要 root）：

```bash
sudo loginctl enable-linger $(id -un)
```

## 常用运维命令（全部是用户级）

```bash
systemctl --user status <slug>            # 状态
journalctl --user -u <slug> -f            # 日志
systemctl --user restart <slug>           # 重启
systemctl --user list-timers | grep <slug>  # 定时器（备份/维护，同步没有定时器）
sh deploy/setup-node.sh --check           # Node 与 pi 现状
sh deploy/install.sh --no-deps            # 只更新代码与单元
sh deploy/uninstall.sh                    # 卸载（保留数据）
sh deploy/uninstall.sh --purge            # 卸载并删除数据（谨慎）
```

## 外部反向代理（可选）

应用只监听 `127.0.0.1:9810`。要让外网访问，用你自己的 nginx/Caddy 反代即可，
示例见 `deploy/nginx-vhost.conf.example`（含「443 独占」与「按 SNI 分流」两种情形）。
注意 SSE：问答是流式返回，反代要关闭 buffering。

## 知识库同步（git）

- 触发方式只有一个：**有改动就同步**——任何写数据的请求成功后排队，25 秒内的多次改动合并成一次，
  后台执行（远端有新提交就先快进，有新东西就提交并推送）。**没有任何定时器**；
  万一进程正好在合并窗口里被杀掉，下次启动会补一次（`settings` 里的 `kb_sync_pending` 标记）。
- 平时不用管；想立刻推一次就在管理页点「立即同步」，或在服务器上跑 `sh deploy/sync.sh`
  （与网页共用同一份实现与凭据，日志在 `$HOME/.local/state/<slug>/sync.log`）。
- 推送凭据是**运行者自己的** SSH key（`~/.ssh/id_ed25519` 加到 GitHub），也可以在网页里
  改用应用自己生成的部署密钥。失败时看上面那份日志。

## 智能体（可选）

```bash
sh deploy/setup-node.sh                    # 用户级 Node + pi
# 网页「管理 → 智能体权限」生成令牌，写入 ~/.config/<slug>/agent.env（600）
sh deploy/pi/agent.sh -p "现在该注意什么？"   # systemd 用户沙箱里运行
sh deploy/pi/agent.sh                      # 交互式
BG_AGENT_CHECK_ONLY=1 sh deploy/pi/run.sh   # 干跑：只检查配置，不启动模型
```

权限边界见 [[docs/agent-permissions.md]]。端点可以是内网模型，也可以是公网 API
（公网端点启动时会打黄色警告）。

## 资源占用参考

| 组件 | 实测 |
|---|---|
| Web 服务 | 常驻 47–50MB（单元上限 320MB） |
| 智能体（按需启动） | 峰值约 110MB，单次回答约 7.5s |
| 模型推理 | 不在本机（走配置的端点） |

## 排错（迁移/搬迁时最容易踩的四个坑）

1. **用户单元找不到文件**：`systemctl --user` 报 `Unit file ... does not exist`，
   而单元确实在 `~/.config/systemd/user/` 下。原因是**用户管理器缓存了旧的 `$HOME`**
   （例如刚用 `usermod -d` 改过家目录）。修法：`loginctl disable-linger <user>` →
   `loginctl terminate-user <user>` → `loginctl enable-linger <user>`。
2. **`usermod` 报 `user ... is currently used by process`**：用户管理器正以该用户运行。
   先 `loginctl terminate-user <user>` 再改，改完重新 enable-linger。
3. **服务起来就挂，`status=218/CAPABILITIES`**：用户单元不能应用
   `CapabilityBoundingSet=`、`PrivateDevices=yes`、`ProtectKernelModules=yes`
   （需要特权）。模板里已移除；想加别的沙箱属性，先用
   `systemd-run --user --wait /bin/true -p <属性>` 单独验证。
4. **搬过目录后 Python 环境失效**：`bin/python` 只是符号链接（仍有效），
   但 `bin/pip` 的 shebang 写死了旧路径。`install.sh` 会用 `pip --version` 实测并自动重建。

## 关于日志权限

`journalctl --user` 需要用户属于 `systemd-journal`（或 `adm`）组。运行账号通常是
`nologin` 的专用账号，直接用 root 读更省事：`journalctl _UID=<uid> -n 50`。

## 推送凭据

`deploy/sync.sh` 用**运行账号自己的** SSH key 推送。若该账号没有 key，
先在它自己的 shell 里 `ssh-keygen -t ed25519`，再把公钥加到 GitHub
（Deploy key 或账号 SSH keys）。失败信息写在 `$HOME/.local/state/<slug>/sync.log`。
