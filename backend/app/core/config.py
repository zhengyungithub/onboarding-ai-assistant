from pydantic_settings import BaseSettings
from cryptography.fernet import Fernet
from functools import lru_cache


def _default_fernet_key() -> str:
    """生成一个开发用的默认 Fernet key（生产环境请通过环境变量覆盖）"""
    return Fernet.generate_key().decode()


class Settings(BaseSettings):
    """应用配置，所有敏感项从环境变量读取"""

    # 数据库
    DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5432/hr_ai"

    # Qdrant
    QDRANT_URL: str = "http://localhost:6333"

    # JWT
    JWT_SECRET_KEY: str = "dev-secret-key-change-in-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440  # 24小时

    # CORS 允许的前端来源（逗号分隔，如 https://hr.example.com,https://www.example.com）
    # 留空 = 开发模式放行所有来源（本机开发用）；生产环境必须显式配置
    CORS_ORIGINS: str = ""

    # API Key 加密（Fernet key，32位 url-safe base64）
    API_KEY_ENCRYPTION_KEY: str = _default_fernet_key()

    # 管理员初始化
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "change-me-on-first-login"  # 占位：真实密码走 .env 的 ADMIN_PASSWORD
    ADMIN_EMAIL: str = "admin@example.com"

    # 默认 LLM 配置（兜底）
    DEFAULT_LLM_BASE_URL: str = "https://api.deepseek.com"
    DEFAULT_LLM_API_KEY: str = ""
    DEFAULT_LLM_MODEL: str = "deepseek-chat"
    DEFAULT_EMBEDDING_API_KEY: str = ""
    DEFAULT_EMBEDDING_MODEL: str = "bge-small-zh-v1.5"

    # 邮件 SMTP 配置（用于忘记密码验证码）
    SMTP_HOST: str = "smtp.qq.com"
    SMTP_PORT: int = 465
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = ""

    # ---- Agent（M1：ReAct 单 Agent + 灰度开关）----
    # off    = 完全走原有固定管线（默认值，行为零变化）
    # shadow = 用户仍看固定管线结果，Agent 在后台只读跑一遍并记录轨迹（用于质量对比）
    # on     = 用户走 Agent 路径
    AGENT_MODE: str = "off"
    # 单次运行的硬预算（超出即停止补检索，用已有证据直接起草）
    AGENT_MAX_TOOL_CALLS: int = 4
    AGENT_MAX_LLM_CALLS: int = 6
    AGENT_TIMEOUT_SECONDS: int = 45
    # 单次规划调用的软超时：超时视为「规划失败」，带着已有证据直接起草（不阻塞用户）
    AGENT_PLAN_TIMEOUT_SECONDS: int = 20
    AGENT_SEARCH_TOP_K: int = 8
    # 快路径：预检索命中数达到该值即认为证据充足，跳过规划模型调用直接起草
    # （实测省下一次模型调用 ≈ 数秒；证据薄的时候仍会走完整的规划循环）
    AGENT_FAST_PATH_MIN_HITS: int = 5
    # shadow 模式的采样率（1.0=每条都跑；调小可控制对比期的 token 成本）
    AGENT_SHADOW_RATE: float = 1.0
    # 是否启用 LangGraph 检查点（图状态持久化 + 中断恢复；启用 HITL 时会自动打开）
    AGENT_USE_CHECKPOINT: bool = False
    # 检查点数据库路径（留空 = backend/data/agent_checkpoints.db）
    AGENT_CHECKPOINT_PATH: str = ""
    # 轨迹表最多保留多少条（shadow 模式会产生较多记录）
    AGENT_RUN_KEEP: int = 2000
    # 写操作人工审批（HITL）：模型要执行下列工具时，图会先中断并等人工批准
    # 默认关闭 —— 关闭时行为与改造前完全一致（模型拿不到这些写工具）
    AGENT_HITL_ENABLED: bool = False
    # 需要人工审批的工具（逗号分隔）
    AGENT_HITL_TOOLS: str = "create_draft_item,update_item"

    # ---- 引用校验（Verifier / 改造 3）----
    # 默认开启：校验只读、不阻塞；关掉即回到改造前的行为（一键回滚）
    AGENT_VERIFIER_ENABLED: bool = True
    # 生成前校验（问题 ↔ 证据匹配度）：不合格会触发"回炉补检索"
    AGENT_VERIFIER_REPAIR_ENABLED: bool = True
    # 规则覆盖率 ≥ 该值判通过（直接进定稿）
    AGENT_VERIFIER_PASS_THRESHOLD: float = 0.85
    # 规则覆盖率 < 该值判不合格（回炉补检索 / 生成后降级标记）
    AGENT_VERIFIER_REPAIR_THRESHOLD: float = 0.55
    # 最多回炉几轮（回炉会消耗工具预算，且用户要等）
    AGENT_VERIFIER_MAX_REPAIR_ROUNDS: int = 2
    # 规则存疑时是否再调一次模型逐句复核（默认关：规则能覆盖绝大多数情况，省 token）
    AGENT_VERIFIER_USE_LLM: bool = False
    # LLM 复核的软超时（秒）：超时按"放行"处理，绝不拖住问答
    AGENT_VERIFIER_LLM_TIMEOUT: int = 15
    # 过滤引用时至少保留几条（避免"答案有内容但引用列表空"的观感问题）
    AGENT_VERIFIER_MIN_CITATIONS: int = 1

    # ---- 三层记忆（改造 4）----
    # 总开关。**设为 false 时行为与改造前一字不差**：短期历史回到 4 轮、
    # 不注入摘要、不做历史召回、不抽取偏好、不采集未答问题（一键回滚）。
    AGENT_MEMORY_ENABLED: bool = True
    # L1 会话短期：最近几轮原文进 prompt（改造前固定 4 轮）
    AGENT_MEMORY_SHORT_TURNS: int = 6
    # L1.5 会话摘要：会话累计消息数达到该阈值才第一次生成摘要
    # （12 轮 × 2 条 = 24 条；低于此值的会话完全不触发，零额外成本）
    AGENT_MEMORY_SUMMARY_TRIGGER_TURNS: int = 12
    # 之后每新增这么多轮未覆盖的消息，才增量刷新一次摘要
    AGENT_MEMORY_SUMMARY_INTERVAL_TURNS: int = 6
    # 摘要正文最大字符数（防止摘要本身把上下文吃满）
    AGENT_MEMORY_SUMMARY_MAX_CHARS: int = 800
    # 摘要生成的软超时（秒）：超时放弃本次刷新，绝不影响用户问答
    AGENT_MEMORY_SUMMARY_TIMEOUT: int = 20
    # L1.6 检索式历史：把历史消息向量化，按当前问题召回最相关的片段
    AGENT_MEMORY_RECALL_ENABLED: bool = True
    AGENT_MEMORY_RECALL_TOP_K: int = 3
    # 召回的最低相似度（低于此值不注入，避免把不相关的旧对话塞进 prompt）
    AGENT_MEMORY_RECALL_MIN_SCORE: float = 0.55
    # 单条召回片段注入 prompt 时的最大字符数
    AGENT_MEMORY_RECALL_MAX_CHARS: int = 300
    # L2 用户偏好：作为检索提示参与查询扩展的词数上限 / 至少出现几次才算偏好
    AGENT_MEMORY_PREFERENCE_TERMS: int = 5
    AGENT_MEMORY_PREFERENCE_MIN_COUNT: int = 2
    # L2 偏好里系统名的白名单（逗号分隔；留空则只用知识库里的流程名做识别）
    AGENT_MEMORY_SYSTEM_TERMS: str = ""
    # L3 未答问题：每个用户最多保留多少条（超出后删最早的）
    AGENT_MEMORY_UNANSWERED_KEEP: int = 500

    # ---- 主动巡检（改造 5）----
    # 巡检是"离线批处理"：不在问答链路上，不影响任何用户请求。关闭只是停用巡检入口。
    AGENT_PATROL_ENABLED: bool = True
    # ① 过期检查：published 且 updated_at 距今超过该天数的条目进入"待复核清单"
    AGENT_PATROL_STALE_DAYS: int = 180
    # 单次巡检各区块最多输出多少条（防止报告本身过大）
    AGENT_PATROL_MAX_ITEMS: int = 50
    # ② 缺口聚类：每次最多取多少条未答问题参与聚类
    AGENT_PATROL_GAP_MAX_QUESTIONS: int = 200
    # 余弦相似度 ≥ 该值归为同一簇
    AGENT_PATROL_GAP_SIMILARITY: float = 0.8
    # 簇内问题数 ≥ 该值才算"有效缺口"（1~2 次提问更像个案，不值得一提）
    AGENT_PATROL_GAP_MIN_CLUSTER: int = 3
    # 单次巡检最多产出多少个缺口簇（按簇大小降序取前 N）
    AGENT_PATROL_GAP_MAX_CLUSTERS: int = 10
    # 是否为缺口簇生成草稿条目（关闭则只出报告不建草稿；模型不可用时也不会建草稿）
    AGENT_PATROL_DRAFT_ENABLED: bool = True
    # ③ 质量异常：被引用次数下限 + 有用率上限 + 至少需要几条反馈才判定
    AGENT_PATROL_QUALITY_MIN_CITED: int = 5
    AGENT_PATROL_QUALITY_MAX_USEFUL_RATE: float = 0.5
    AGENT_PATROL_QUALITY_MIN_FEEDBACK: int = 2
    # 质量统计时最多回溯多少条回答消息（防止全表扫描）
    AGENT_PATROL_QUALITY_MAX_MESSAGES: int = 3000
    # 质量异常默认用规则生成改进建议（零 token）；开启后额外让模型指出可能过时的步骤
    AGENT_PATROL_QUALITY_LLM: bool = False
    # ④ 一致性检查（流程节点描述 ↔ 关联条目内容，需 LLM 判定；默认关，成本最高）
    AGENT_PATROL_CONSISTENCY_ENABLED: bool = False
    AGENT_PATROL_CONSISTENCY_MAX_PAIRS: int = 20
    # 巡检内部 LLM 调用的软超时（秒）：超时就放弃该处增强，不影响其余区块
    AGENT_PATROL_LLM_TIMEOUT: int = 30
    # 巡检报告保留条数（超出后删最早的）
    AGENT_PATROL_REPORT_KEEP: int = 200

    # ---- 文档确认清单（迭代 4）----
    # 免登录确认通道：确认人通常不装系统、也不一定有账号，没有这条通道协同推不动。
    # **默认关闭**：开启后 /api/public/confirmations/* 才可访问（关闭时一律 404，不暴露存在性）。
    DOC_CONFIRM_PUBLIC_ENABLED: bool = False
    # 免登录链接有效期（天）：过期后需重新发起
    DOC_CONFIRM_TOKEN_TTL_DAYS: int = 7

    class Config:
        env_file = ".env"
        case_sensitive = True


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
