
-- 变更说明（工作汇报 Wiki 场景）：
--   * 原 project_id 全部移除：每用户独立构建 Wiki，不再有项目/知识库概念。
--   * 原 kb_file_id / kb_file_version_id 重命名为 report_id / report_version_id：
--     来源即工作汇报，report_id 即汇报 record id。
--   * 版本号改为「更新时间/内容驱动」：汇报无显式版本号，重新拉取时按
--     content_hash 是否变化决定是否递增 version_id（file_version_chain 记录）。

SET NAMES utf8mb4;
SET time_zone = '+00:00';

-- ============================================================
-- 一、入库 / 版本 / 事件
-- ============================================================

CREATE TABLE IF NOT EXISTS ingest_batch (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    emp_id           BIGINT NOT NULL,
    batch_name      VARCHAR(255) NOT NULL,
    doc_count       INT NOT NULL DEFAULT 0,
    status          TINYINT NOT NULL DEFAULT 0 COMMENT '0 pending 1 running 2 done 3 failed',
    created_at      DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    finished_at     DATETIME(3) NULL,
    INDEX idx_batch_emp (emp_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS ingest_event_log (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    batch_id        BIGINT NULL,
    emp_id           BIGINT NOT NULL,
    report_id       BIGINT NOT NULL,
    event_type      VARCHAR(64) NOT NULL COMMENT 'parse/chunk/embed/index',
    status          TINYINT NOT NULL DEFAULT 0,
    detail          JSON NULL,
    created_at      DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    INDEX idx_evt_file (emp_id, report_id),
    INDEX idx_evt_batch (batch_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS event_log (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    emp_id           BIGINT NOT NULL,
    actor           VARCHAR(128) NULL,
    event_type      VARCHAR(64) NOT NULL,
    payload         JSON NULL,
    created_at      DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    INDEX idx_log_type (emp_id, event_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 文档版本链：每个汇报可有多个版本（按内容 hash 驱动），权限/检索均精确到版本
CREATE TABLE IF NOT EXISTS file_version_chain (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    emp_id           BIGINT NOT NULL,
    report_id       BIGINT NOT NULL,
    version_id      BIGINT NOT NULL,
    parent_version_id BIGINT NULL,
    file_name       VARCHAR(512) NOT NULL,
    content_hash    VARCHAR(64) NULL,
    status          TINYINT NOT NULL DEFAULT 1 COMMENT '1 active 2 superseded 3 deleted',
    authority_level TINYINT NOT NULL DEFAULT 1 COMMENT '1 low 2 mid 3 high',
    created_at      DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    UNIQUE KEY uk_file_version (emp_id, report_id, version_id),
    INDEX idx_fvc_emp_file (emp_id, report_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ============================================================
-- 二、Grant 权限（文件/claim 级，唯一权限事实源）
-- ============================================================

-- 权限审计：三重鉴权每一阶段都留痕（运行时鉴权基于外部传入的白名单，不缓存 grant）
CREATE TABLE IF NOT EXISTS grant_audit (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    request_id      VARCHAR(64) NOT NULL,
    emp_id           BIGINT NOT NULL,
    user_id         BIGINT NOT NULL,
    phase           VARCHAR(24) NOT NULL COMMENT 'pre_retrieval/post_retrieval/pre_llm',
    target_type     VARCHAR(16) NOT NULL COMMENT 'file/chunk/claim',
    target_id       VARCHAR(64) NOT NULL,
    action          VARCHAR(16) NOT NULL,
    allowed         TINYINT NOT NULL,
    denied_reason   VARCHAR(128) NULL,
    denied_refs     JSON NULL COMMENT '被拒绝的来源引用列表',
    created_at      DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    INDEX idx_audit_req (request_id),
    INDEX idx_audit_user_phase (emp_id, user_id, phase)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ============================================================
-- 三、Wiki 编译 / Claim / 证据组
-- ============================================================

CREATE TABLE IF NOT EXISTS wiki_compilation_task (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    emp_id           BIGINT NOT NULL,
    report_id       BIGINT NOT NULL,
    version_id      BIGINT NOT NULL,
    status          TINYINT NOT NULL DEFAULT 0 COMMENT '0 pending 1 running 2 done 3 failed',
    quality_gate    JSON NULL,
    created_at      DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    finished_at     DATETIME(3) NULL,
    INDEX idx_task_file (emp_id, report_id, version_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS wiki_page (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    emp_id           BIGINT NOT NULL,
    folder_id       BIGINT NOT NULL DEFAULT 0 COMMENT '归属文件夹，0=根；仅导航用途，非安全边界',
    page_key        VARCHAR(128) NULL COMMENT '页面稳定 slug（命名空间：summary-<id>/topic/<slug>/entity/<slug>/concept/<slug>/index）；与 emp_id 组成幂等键',
    page_type       VARCHAR(32) NOT NULL DEFAULT 'topic' COMMENT 'entity/concept/topic/summary/index',
    title           VARCHAR(512) NOT NULL,
    revision        INT NOT NULL DEFAULT 1,
    markdown         MEDIUMTEXT NOT NULL,
    summary         TEXT NULL,
    links           JSON NULL COMMENT '页面正文中的 [[slug]] 内部链接目标列表',
    status          TINYINT NOT NULL DEFAULT 1 COMMENT '1 active 2 archived',
    is_official     TINYINT NOT NULL DEFAULT 0 COMMENT '0 自动蒸馏 1 正式发布(独立 official_grant)',
    official_grant  JSON NULL,
    created_at      DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at      DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    UNIQUE KEY uk_page_key (emp_id, page_key),
    INDEX idx_page_emp (emp_id, status),
    INDEX idx_page_official (emp_id, is_official),
    INDEX idx_page_folder (emp_id, folder_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 页面与源文件的关联（一篇 Wiki 页面可来源于多篇汇报）。
-- 用于「按 report_id 过滤」：页面可发现性 = 至少 1 个源文件在白名单内。
CREATE TABLE IF NOT EXISTS wiki_page_source (
    id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
    emp_id           BIGINT NOT NULL,
    page_id             BIGINT NOT NULL,
    report_id          BIGINT NOT NULL,
    report_version_id  BIGINT NOT NULL,
    created_at          DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    UNIQUE KEY uk_page_src (emp_id, page_id, report_id, report_version_id),
    INDEX idx_ps_file (emp_id, report_id, report_version_id),
    INDEX idx_ps_page (emp_id, page_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Wiki 页面事实（Claim）相关表已移除：无权限场景不再抽取 claim 细粒度证据。
-- （原 wiki_claim / claim_support_group / claim_support_source / claim_source 四表已删）

-- ============================================================
-- 四、Topic / 聚类（L1，预留）
-- ============================================================

CREATE TABLE IF NOT EXISTS topic (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    emp_id           BIGINT NOT NULL,
    name            VARCHAR(255) NOT NULL,
    description     TEXT NULL,
    created_at      DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    INDEX idx_topic_emp (emp_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS topic_document (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    emp_id           BIGINT NOT NULL,
    topic_id        BIGINT NOT NULL,
    report_id       BIGINT NOT NULL,
    report_version_id BIGINT NOT NULL,
    created_at      DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    UNIQUE KEY uk_topic_doc (topic_id, report_id, report_version_id),
    INDEX idx_td_topic (topic_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ============================================================
-- 六、Wiki 文件夹树 + Wiki 事件日志
-- ============================================================

-- Wiki 页面的一级导航树
-- 设计要点：
--   * 仅用于组织/导航，**不是安全边界**——安全性由 Claim 级证据组授权判定
--   * path 物化存全路径（如 /root/child/...），depth 冗余加速查询与排序。
--   * page_count 冗余计数，由编译/移动维护，避免统计时实时聚合整棵树。
CREATE TABLE IF NOT EXISTS wiki_folders (
    id                BIGINT AUTO_INCREMENT PRIMARY KEY,
    emp_id         BIGINT NOT NULL,
    parent_id         BIGINT NOT NULL DEFAULT 0 COMMENT '0 表示根目录',
    name              VARCHAR(255) NOT NULL,
    path              VARCHAR(1024) NOT NULL DEFAULT '/' COMMENT '物化全路径',
    depth             INT NOT NULL DEFAULT 0,
    sort_order        INT NOT NULL DEFAULT 0,
    page_count        INT NOT NULL DEFAULT 0,
    created_at        DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at        DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    UNIQUE KEY uk_emp_name_parent (emp_id, parent_id, name),
    INDEX idx_wf_parent (emp_id, parent_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 只追加（append-only）的 Wiki 事件日志。
-- 设计要点：
--   * 替代“单 TEXT 列塞全量日志”做法（后者读全量需 O(n^2) 解析且无法分页）；
--     每次事件 = 一条 INSERT，读取按 (emp_id, id DESC) 分页。
--   * 与通用 event_log / grant_audit 区分：本表聚焦 Wiki 生命周期事件，
--     便于回溯某次编译/发布/投影/移动对哪些页面产生了影响。
CREATE TABLE IF NOT EXISTS wiki_log_entries (
    id                BIGINT AUTO_INCREMENT PRIMARY KEY,
    emp_id         BIGINT NOT NULL,
    action            VARCHAR(64) NOT NULL COMMENT 'wiki.compile/wiki.publish/wiki.reproject/wiki.move/wiki.grant/wiki.revoke',
    knowledge_id      VARCHAR(64) DEFAULT '' COMMENT '关联 page_id / folder_id 等',
    doc_title         VARCHAR(512) DEFAULT NULL,
    summary           TEXT,
    pages_affected    JSON DEFAULT NULL COMMENT '受影响页面列表等结构化信息',
    created_at        DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    INDEX idx_wle_emp_id (emp_id, id DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 单篇汇报摘要独立存储表（与 Wiki 页面解耦）。
-- 设计要点：
--   * 一篇汇报(report_id)全局唯一，因此「按人各生成一份摘要」是冗余的；摘要以 report_id 粒度只存一条，
--     由 Wiki 与各消费方共享，不需要 emp_id 维度。
--   * 摘要可不由 Wiki 构建负责：工作协同系统等外部生产者写入本表，Wiki 仅消费。
--   * summary_status 字段驱动异步重算：汇报更新后由上游（或异步任务）将状态置为 0(pending)，
--     下次 Wiki 构建（或外部调度）检测到 pending / 缺失 / content_hash 不匹配时重新生成。
--   * 本表只存「汇报级摘要」；向量由 l0_ingest / 工作协同系统写入 ES。
CREATE TABLE IF NOT EXISTS report_summary (
    id                BIGINT AUTO_INCREMENT PRIMARY KEY,
    report_id         BIGINT NOT NULL,
    version_id        BIGINT NOT NULL DEFAULT 0 COMMENT '最近一次生成摘要所基于的汇报版本',
    title             VARCHAR(512) DEFAULT NULL,
    summary           TEXT COMMENT '单篇汇报的一句话/段落级摘要',
    markdown          MEDIUMTEXT COMMENT '汇报级编译正文（Wiki 摘要页来源），缺失时由 Wiki fallback 生成',
    entities          JSON DEFAULT NULL COMMENT '该篇汇报级提炼的 entity 候选列表(JSON)，由工作协同系统或 Wiki 兜底一次生成',
    concepts          JSON DEFAULT NULL COMMENT '该篇汇报级提炼的 concept 候选列表(JSON)，同上',
    content_hash      VARCHAR(64) DEFAULT NULL COMMENT '生成摘要时汇报正文的 hash；不匹配则视为 stale',
    summary_status    TINYINT NOT NULL DEFAULT 0 COMMENT '0 pending/待生成 1 done/已生成 2 stale/已过期待重算',
    generated_at      DATETIME(3) NULL,
    created_at        DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at        DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    UNIQUE KEY uk_report (report_id),
    INDEX idx_rs_status (summary_status),
    INDEX idx_rs_hash (report_id, content_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
