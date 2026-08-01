# 贡献指南

本项目规划为多人协作。协作者请遵守以下约定。

## Ticket 工作流

工作以 ticket 为单位组织。**GitHub Issue 是 ticket 的权威**：编号、状态、讨论都在 Issue 上进行，Phase 对应 Milestone。

- 开工前：在本仓库建 Issue（或认领已有 Issue），关联对应 Phase 的 Milestone
- 过程文档（踩坑记录、调研笔记等大段材料）：按需建本地 ticket 目录，不是每个 ticket 都需要
- **目录名**：`YYYYMMDD-<kebab-slug>/`，slug 为 3-6 个小写单词；同一天多个加后缀（`20260801-xxx-b`）
- 目录内 `README.md` 第一行写 `Issue: CoCoCoDeDe/home-monitor#N`，双向链接
- 完成即归档不动，不搞 `done/` 子目录
- commit message 用 `closes #N` / `refs #N` 关联 Issue

> 本仓库为**公开仓库**：Issue 和代码对所有人可见，请勿在 Issue、代码、文档中写入住址、户型、WiFi 密码、内网地址等敏感信息。

## 文档归属

只有**项目通用、长期、广度**的文档才能进本仓库（如架构设计、硬件清单、部署手册），存 `docs/`。个人上下文、临时记录、调研笔记不进本仓库。
