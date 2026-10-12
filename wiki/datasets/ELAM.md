---
id: dataset:25f7424dd8f9ec4d
type: dataset
title: ELAM
sources:
- b4a986af06bbd7de0dc1869731b32421a4c2914950fa34f0a57aebfbc2deb0f0
generated_by: researchwiki.compiler.v1
schema_version: 1
---

# ELAM

## 稳定问题

ELAM 在这些论文中如何定义、使用，其证据和适用边界是什么？

## 数据集在不同研究中的用途

### [[Uncertainty-aware evidential learning for legal case retrieval with noisy correspondence]]

Legal case retrieval dataset with 85 queries and 50 candidate cases per query. [1]

用途：Benchmark evaluation [1]

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

来源：`raw/papers/b4a986af06bb-Uncertainty-aware-evidential-learning-for-legal-case-retrieval-with-noisy-correspondence.pdf` · 第 9 页 · 第 1 段 · `page:9#chars=6-686` · 原文命中

> ation Sciences 702 (2025) 121915
> 9
> W. Qin, W. Yu, K. Zhang et al.
> Table 1
> Statistics of LeCaRD and ELAM.
> LeCaRD
> ELAM
> # total queries
> 107
> 85
> # candidate cases per query
> 100
> 50
> Avg. # relevant cases per query
> 10.33
> 9.06
> Avg. # sentences per query
> 6.91
> 16.24
> Avg. # sentences per candidate case
> 86.63
> 43.43
> Avg. # cited law articles per candidate case
> 6.5
> 5.62
> specific architecture and may not generalize well across diﬀerent models or retrieval tasks. ELCR’s modular design makes it a versatile
> framework that can be applied to a wide range of legal case retrieval tasks with minimal mod­fications.
> Limitation 1: Potential Reduction in Positive Sample Availab

## 我的笔记

<!-- user-notes:start -->

<!-- user-notes:end -->
