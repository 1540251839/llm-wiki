---
id: method:1b8f2c88e5cfdd8c
type: method
title: Multi-faceted relevance modeling
sources:
- b4a986af06bbd7de0dc1869731b32421a4c2914950fa34f0a57aebfbc2deb0f0
generated_by: researchwiki.compiler.v1
schema_version: 1
---

# Multi-faceted relevance modeling

## 稳定问题

Multi-faceted relevance modeling 在这些论文中如何定义、使用，其证据和适用边界是什么？

## 方法在不同研究中的用途

### [[Uncertainty-aware evidential learning for legal case retrieval with noisy correspondence]]

Estimates relevance from neural semantic, concept-level, law-level, and fact-level modules using sentence-concept/law/fact similarity aggregation and a linear fusion layer. [1]

用途：Relevance estimation for uncertainty quantification [1]

该论文的相关结论（需结合原文适用条件）：

- ELCR exhibits robustness against noisy correspondences while maintaining competitive retrieval performance across benchmark datasets. [1]
- Achieves average NDCG improvements of 20.90% on LeCaRD and 10.99% on ELAM compared to various baselines. [1]
- Explicit modeling of legal uncertainty via evidential learning improves reliability and enables effective noise identification. [1]
- Framework is adaptable across different backbone architectures, including BERT, Criminal-BERT, and Lawformer. [1]

该来源的边界：

- In high-noise scenarios, imperfect differentiation between noisy positive samples and true negatives may reduce the number of available positive samples and degrade performance. [1]
- Additional computational overhead from evidence calculation, uncertainty estimation, and label correction may challenge resource-constrained environments. [1]

## 证据边界

各来源的观点分别保留；共享术语不代表实验条件或结论一致。编号指向来源片段，抽取结果仍需对照原文复核。

## 来源证据

### [1] Uncertainty-aware evidential learning for legal case retrieval with noisy correspondence

来源：`raw/papers/b4a986af06bb-Uncertainty-aware-evidential-learning-for-legal-case-retrieval-with-noisy-correspondence.pdf` · 第 5 页 · 第 1 段 · `page:5#chars=75-755` · 原文命中

> g. 2. The overall framework of ELCR includes three main components. The bottom-left illustrates the multi-faceted relevance modeling inspired by legal uncertainty.
> The top-left demonstrates explicit uncertainty modeling through evidential learning. The top-right showcases evidence-based noise detection.
> 3.2. The proposed framework
> To address the issue of noisy correspondence in legal case retrieval, we enhance the existing neural module by introducing a novel
> evidential learning framework, referred to as ELCR. As illustrated in Fig. 2, ELCR first takes a query case 𝑞and a candidate case 𝑐
> as input and predicts their multi-faceted relevance ̂𝑟. Subsequently, ELCR employs

## 我的笔记

<!-- user-notes:start -->

<!-- user-notes:end -->
