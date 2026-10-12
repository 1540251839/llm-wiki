---
id: method:f6c34051157cde75
type: method
title: Label correction strategy
sources:
- b4a986af06bbd7de0dc1869731b32421a4c2914950fa34f0a57aebfbc2deb0f0
generated_by: researchwiki.compiler.v1
schema_version: 1
---

# Label correction strategy

## 稳定问题

Label correction strategy 在这些论文中如何定义、使用，其证据和适用边界是什么？

## 方法在不同研究中的用途

### [[Uncertainty-aware evidential learning for legal case retrieval with noisy correspondence]]

Corrects observed labels with high uncertainty based on relevance evidence, using a warm-up phase and a stabilization threshold to prevent over-correction and training fluctuation. [1]

用途：Training robustness [1]

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

来源：`raw/papers/b4a986af06bb-Uncertainty-aware-evidential-learning-for-legal-case-retrieval-with-noisy-correspondence.pdf` · 第 2 页 · 第 1 段 · `page:2#chars=3715-4180` · 原文命中

> ctuations in estimated uncertainty during the early stages of
> model training, we introduce a novel label correction strategy to prevent over-correction and stabilize learning. Thirdly, in addition
> to traditional relevance-oriented training objectives, we propose two evidence-based loss functions to enhance retrieval performance
> under noisy conditions. The one is an evidence-based contrastive learning loss, which ensures more evidence is generated for correct

## 我的笔记

<!-- user-notes:start -->

<!-- user-notes:end -->
