# 运维 Cheatsheet

> 这份文档记录 Sealos 集群运维的真实操作路径。**遇到与 `deploy/sealos/*.yaml` 描述不一致的地方，以本文档和集群实际为准。**

---

## 0. 基本信息

| 项 | 值 |
|---|---|
| Sealos 区域 | 北京 `https://bja.sealos.run` |
| K8s API Server | `https://bja.sealos.run:6443` |
| Namespace | `ns-3zn44u6p` |
| Deployment 名字 | `backend`、`celery`、`frontend`（**不是** `resell-*` 前缀） |
| 镜像仓库 | `crpi-ryxzfb3l96vqk28a.cn-shenzhen.personal.cr.aliyuncs.com/resell-assistant/*` |
| 镜像 tag 规则 | git commit hash（不是 `:latest`），由 CI 在构建时注入 |

**当前镜像状态（2026-05 时点）**：

- `backend` deployment → `backend:<commit-hash>`
- `celery` deployment → **同样**用 `backend:<commit-hash>`（CI 已统一只构建一份镜像，按 `APP_MODE=celery` 切模式）
- `frontend` deployment → `frontend:<commit-hash>`

---

## 1. 获得 kubectl 操作权限（首次设置）

Sealos 桌面**没有** Terminal 应用，集群运维必须用 kubectl。

### 1.1 在 Sealos 桌面下载 kubeconfig

1. 浏览器打开 https://bja.sealos.run，登录
2. 点右上角头像 → **Kubeconfig** → 浏览器会下载一个 yaml 文件

### 1.2 在 devbox 上配置 kubectl

```bash
# 写入 kubeconfig
mkdir -p ~/.kube
cat > ~/.kube/config <<'EOF'
<贴上下载到的 yaml 文件全部内容>
EOF
chmod 600 ~/.kube/config

# 安装 kubectl（dl.k8s.io 国内被墙，用 daocloud 镜像）
mkdir -p ~/.local/bin
curl -fsSL -o ~/.local/bin/kubectl \
  "https://files.m.daocloud.io/dl.k8s.io/release/v1.28.4/bin/linux/amd64/kubectl"
chmod +x ~/.local/bin/kubectl
export PATH=$HOME/.local/bin:$PATH

# 验证
kubectl get deployment
```

> 注意：kubeconfig 里的 token 有有效期，过期了重新去 Sealos 桌面再下一份覆盖即可。

---

## 2. 常用运维任务

> 所有命令默认 namespace 已通过 kubeconfig 锁定为 `ns-3zn44u6p`，不需要 `-n`。
> kubectl 输出会带 `Warning: Use tokens from the TokenRequest API...`，**忽略即可**，是 Sealos 集群级警告，不影响功能。

### 2.1 查看 pod 状态 / 日志

```bash
kubectl get pod                          # 列出所有 pod
kubectl logs -f deployment/celery --tail=100        # 实时跟踪 celery 日志
kubectl logs -f deployment/backend --tail=100       # 实时跟踪 backend 日志
kubectl logs -f <pod-name> --previous     # 看上一个 crash 的日志
```

### 2.2 进入 pod 执行命令

```bash
kubectl exec -it deployment/celery -- bash
kubectl exec deployment/backend -- python -c 'from app.core.config import get_settings; print(get_settings().DINGTALK_WEBHOOK_URL)'
```

### 2.3 重启 deployment（不改任何配置）

```bash
kubectl rollout restart deployment/celery
kubectl rollout restart deployment/backend
kubectl rollout status deployment/celery     # 等到完成
```

### 2.4 改环境变量（**唯一真正生效的配置入口**）

集群里的 deployment **不引用任何 secret/configmap**，env 直接写在 pod template 里。改环境变量的标准做法：

```bash
kubectl set env deployment/celery deployment/backend \
  KEY1='value1' \
  KEY2='value2'
```

这条命令会自动触发滚动重启。**不需要**改 yaml、不需要重新 build 镜像。

#### 例：换钉钉机器人

```bash
kubectl set env deployment/celery deployment/backend \
  DINGTALK_WEBHOOK_URL='https://oapi.dingtalk.com/robot/send?access_token=xxxxx' \
  DINGTALK_SECRET='SECxxxxx'
```

> `config.py` 用 pydantic `BaseSettings`，环境变量同名字段会自动覆盖默认值。

#### 例：删除某个环境变量

```bash
kubectl set env deployment/celery KEY1-       # 注意末尾的 - 号
```

### 2.5 切换镜像（紧急情况下）

```bash
kubectl set image deployment/celery \
  celery=crpi-ryxzfb3l96vqk28a.cn-shenzhen.personal.cr.aliyuncs.com/resell-assistant/backend:<commit-hash>
```

容器名（`celery=` 前面这个）以 `kubectl get deployment celery -o yaml | grep '  - name:'` 为准。

### 2.6 查看当前所有环境变量

```bash
kubectl get deployment celery -o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}'
```

---

## 3. 常见 / 应急场景

### 3.1 钉钉机器人换号

走 §2.4 里"换钉钉机器人"那条命令，一行解决，不要去动代码、yaml 或镜像。

### 3.2 后端代码改了想让线上生效

镜像 tag 是 commit hash，**必须**走 CI 出新镜像 → 用 §2.5 切镜像。
不要试图在 pod 里直接改文件，pod 重启会丢。

### 3.3 celery 跑得不正常想看在做什么

```bash
kubectl logs -f deployment/celery --tail=200
# 或进 pod 看 celery inspect
kubectl exec -it deployment/celery -- celery -A app.core.celery_app:celery_app inspect active
```

### 3.4 数据库 / Redis 连不上

backend 和 celery 通过 service DNS 访问：
- PostgreSQL: `resell-manager-postgresql.ns-3zn44u6p.svc:5432`
- Redis: `resell--manager-redis-redis.ns-3zn44u6p.svc:6379`（注意有两个 `--`）

如果连不上，先看这两个 svc 是否存在：

```bash
kubectl get svc | grep -E "postgresql|redis"
kubectl get pod | grep -E "postgresql|redis"
```

---

## 4. 已知坑（必读）

### 4.1 `deploy/sealos/*.yaml` 与集群严重脱节

仓库里的 yaml 文件描述的是**早期设计**，与集群实际有以下不一致：

| 项 | yaml 写的 | 集群实际 |
|---|---|---|
| Deployment 名字 | `resell-celery` / `resell-backend` | `celery` / `backend` |
| envFrom secret | 引用 `resell-secret` | **没有**这个 secret，env 直接写 |
| image 引用 | `:latest` 占位 | 实际用 `:<commit-hash>`（CI 注入） |

**禁止操作**：

```bash
# ❌ 千万不要跑这个，会创建第二套 deployment 或破坏现有部署
kubectl apply -f deploy/sealos/
```

如果有一天要让 yaml 和集群对齐，应该先 `kubectl get -o yaml` 导出当前真实状态再回写到 yaml。

### 4.2 ~~celery 镜像 404~~（2026-05 已修复）

历史问题：`.github/workflows/build-and-push.yml` 只构建 `backend` 和 `frontend` 镜像，但 deploy 阶段对 celery 引用了从未构建的 `celery:<sha>` tag。旧 pod 因 kubelet 本地镜像缓存还活着，新 pod 拉镜像就 404。

**修复方案（已实施）**：CI 永久只构建 `backend` 一份镜像，celery deployment 复用 backend 镜像，按容器内 `APP_MODE=celery` 环境变量切模式。

涉及变更：
- 删除 `backend/Dockerfile.celery`（不再需要）
- `.github/workflows/build-and-push.yml` 的 `deploy_app "celery"` 第三参数传 `backend`，改用 backend 镜像 tag
- `docker-compose.yml` 的 celery service 改用 backend Dockerfile + 加 `APP_MODE=celery`
- `deploy/sealos/celery-deployment.yaml` 同步更新

### 4.3 钉钉配置历史散落 3 处

| 位置 | 状态 |
|---|---|
| `backend/app/core/config.py` 默认值 | 已是新机器人，但**镜像里**还是旧值 |
| `deploy/sealos/secret.yaml` | 已是新机器人，但**根本没 apply 到集群** |
| 集群 deployment env | 已是新机器人，**唯一真正生效** |

未来换号只用动**第三处**（§2.4 里那条命令）。代码和 yaml 改不改都行，因为它们的值不会跑到生产。

### 4.4 PodSecurity 警告

`kubectl set env / set image` 时会出现：

```
Warning: would violate PodSecurity "restricted:v1.25": ...
```

这是 Sealos 集群的 namespace 级 PodSecurity 设了 restricted profile，但当前 deployment 没满足。**不影响这次操作**——deployment 已经在跑，集群只是审计性警告，不会拒绝更新。如果未来想消除，需要给容器加 `securityContext.runAsNonRoot=true` 等字段，与本指南无关。

### 4.5 devbox 上的孤儿 celery worker（2026-05 修复留底）

历史现象：在 devbox 上做开发时如果跑过 `celery -A app.core.celery_app:celery_app worker`，**进程不会随终端关闭而退出**，会在后台继续连同一个 Redis broker 拉任务。

危害：

- devbox 跑的代码可能是当时本地未提交的旧版本——**它会"偷"K8s 那边的任务并用旧代码执行**
- 结果：你以为在测试 K8s 部署的新镜像，但有 ~50% 概率被 devbox 接走，看到的现象跟新代码无关
- 灾难场景：A/B 测试结论失真、bug 怀疑错地方

**检查 + 清理**：

```bash
# 在 devbox 看是不是有
ps -ef | grep -E "celery.*worker" | grep -v grep

# 看集群里所有连着的 worker（应该只有 K8s celery pod 那一个）
kubectl exec deployment/backend -- python3 -c "
from app.core.celery_app import celery_app
print(list((celery_app.control.inspect(timeout=5).active() or {}).keys()))
"
# 期望输出形如：['celery@celery-859f654988-xxxxx']
# 如果还有 'celery@zmzs001' 之类的 devbox hostname，立刻 kill

# 杀掉本地 worker
pkill -f "celery.*app.core.celery_app"
```

**规范**：在 devbox 做任何 celery 相关开发，**收尾必须 `pkill -f celery`**。

---

## 5. 我可以做、绝对不能做的命令清单

### ✅ 安全
- `kubectl get / describe / logs`
- `kubectl exec`（不修改容器内文件）
- `kubectl set env`、`kubectl set image`
- `kubectl rollout restart / status / undo`

### ⚠️ 慎做（理解清楚再做）
- `kubectl edit deployment/...`（直接编辑 live spec）
- `kubectl scale deployment/celery --replicas=N`（celery 多副本可能引起任务重复消费）

### ❌ 绝不要做
- `kubectl apply -f deploy/sealos/`（见 §4.1）
- `kubectl delete deployment/...`（删了得手动重建）
- `kubectl delete namespace ns-3zn44u6p`（删除整个项目）

---

## 5.5 V3/V4：账号指纹 + frozen cookies（2026-05 上线）

### 5.5.1 它在做什么

每个爬虫账号都有一组**稳定的伪硬件画像** + **冻结的平台设备 cookie**，都存在 `accounts.fingerprint` JSON 字段里（无 schema 变更）：

```json
{
  "version": 1,
  "hardware_concurrency": 4,
  "device_memory": 8,
  "screen": {"width": 1536, "height": 864, "color_depth": 24},
  "platform_str": "Win32",
  "frozen_cookies": {
    "pdd":   {"_nano_fp": "...", "api_uid": "..."},
    "1688":  {"cna": "...", "_tb_token_": "..."}
  }
}
```

- **`hardware_concurrency` / `device_memory` / `screen` / `platform_str`**——同一账号每次浏览器跑出来都是这套，不再全员 8/8
- **`frozen_cookies[<platform>]`**——首次成功爬取后自动捕获 PDD `_nano_fp` 等设备级 cookie，下次进库前覆盖账号库里的旧值，让"设备身份"跨会话稳定

### 5.5.2 查看现状

```bash
# 看全部账号的指纹分布
kubectl exec deployment/backend -- python3 scripts/init_account_fingerprints.py

# 输出示例（hw=hardware_concurrency, mem=device_memory）
# pdd_crawler_0043  pdd_crawler   4  8  1536x864    Win32
```

### 5.5.3 新账号入库后

`get_or_init_fingerprint` 会在该账号第一次被 pick 时**自动**生成并落库，不需要人工介入。但你也可以提前批量回填，避免第一次爬取时多一次 DB 写：

```bash
kubectl exec deployment/backend -- python3 scripts/init_account_fingerprints.py --auto --apply
```

### 5.5.4 强制重抽某个账号的指纹

如果某账号被分到了一组太冷门的值（比如 16 核 / 4K 屏 / MacIntel 这种概率组合），偶尔需要重抽：

```bash
kubectl exec deployment/backend -- python3 scripts/init_account_fingerprints.py \
  --reroll pdd_crawler_0043 --apply
```

`frozen_cookies` 会保留（只重抽硬件）。

### 5.5.5 frozen_cookies 一直是空怎么办

`frozen_cookies` **只在成功爬取（`active_listings > 0`）时**才自动写入。如果你看到 `frozen: none-yet` 一直填不上，原因一定是：

1. 该账号的 cookies 已过期，爬取本身在登录墙截止（PDD H5 跳 login.html）
2. 青果代理这一时段网络不稳，goto 一直 timeout
3. 该账号被 quarantine 了，根本没被 pick

排查顺序：

```bash
# 1. 看池现状
kubectl exec deployment/backend -- python3 scripts/list_crawler_accounts.py

# 2. 看该账号最近一次 cooldown 原因（日志）
kubectl logs deployment/celery --since=2h | grep "quarantined" | grep <account_id>

# 3. 单号体检（不走 V4 自动 freeze，纯探活）
kubectl exec deployment/backend -- python3 scripts/test_pdd_account.py --suffix 0043
```

### 5.5.6 紧急关闭 V4 freeze（如果某天怀疑它引起问题）

V4 的入口在 `app/tasks/selection.py` 的 `_pdd_call` / `_1688_call`，注释掉两行就行（`merge_frozen_into` 和 `freeze_platform_cookies`）。V3 不能关——关了等于回退到全 8/8。

完整禁用方案：把 `accounts.fingerprint` 整列设为 NULL，所有路径会 fallback 到 V3/V4 之前的 8/8 默认行为：

```bash
kubectl exec deployment/backend -- python3 scripts/init_account_fingerprints.py --reset --apply
```

---

## 6. 这份文档之外的事

- 改代码 / 上新功能 → 走 git 提交 + CI 出新镜像 → §2.5 切镜像
- 改前端 → frontend deployment 也是同样流程
- 改 redis/postgres 配置 → 这俩是 KubeBlocks 托管的，不在 namespace 普通 deployment 里，操作走 Sealos 桌面的 Database 应用
