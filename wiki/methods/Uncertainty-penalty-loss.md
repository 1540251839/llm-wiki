---
id: method:35d2c402eb6ca2d8
type: method
title: Uncertainty penalty loss
sources:
- b4a986af06bbd7de0dc1869731b32421a4c2914950fa34f0a57aebfbc2deb0f0
generated_by: researchwiki.compiler.v1
schema_version: 1
---

# Uncertainty penalty loss

## 稳定问题

Uncertainty penalty loss 在这些论文中如何定义、使用，其证据和适用边界是什么？

## 方法在不同研究中的用途

### [[Uncertainty-aware evidential learning for legal case retrieval with noisy correspondence]]

Applies a KL divergence regularization term between the predictive Dirichlet distribution and a uniform Dirichlet to penalize divergences from uncertain retrievals. [1]

用途：Training objective [1]

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

来源：`raw/papers/b4a986af06bb-Uncertainty-aware-evidential-learning-for-legal-case-retrieval-with-noisy-correspondence.pdf` · 第 3 页 · 第 1 段 · `page:3#chars=30-710` · 原文命中

> ) 121915
> 3
> W. Qin, W. Yu, K. Zhang et al.
> predictions than for misclass­fications. The other is an uncertainty penalty loss, which further regularizes the predictive distribution by
> penalizing divergences from uncertain retrievals. Extensive experiments on various noise proportions across two benchmark datasets
> demonstrate that our method exhibits robustness against noisy correspondences while maintaining competitive retrieval performance.
> We summarize our contributions as follows:
> • Inspired by the uncertainty in the legal domain, we propose an evidential learning framework, namely ELCR, to explicitly model
> multi-faceted relevance uncertainties and detect noisy label

## 我的笔记

<!-- user-notes:start -->

<!-- user-notes:end -->
