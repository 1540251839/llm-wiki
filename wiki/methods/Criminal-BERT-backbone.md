---
id: method:331a472eaf6ebc80
type: method
title: Criminal-BERT backbone
sources:
- b4a986af06bbd7de0dc1869731b32421a4c2914950fa34f0a57aebfbc2deb0f0
generated_by: researchwiki.compiler.v1
schema_version: 1
---

# Criminal-BERT backbone

## 稳定问题

Criminal-BERT backbone 在这些论文中如何定义、使用，其证据和适用边界是什么？

## 方法在不同研究中的用途

### [[Uncertainty-aware evidential learning for legal case retrieval with noisy correspondence]]

Implements the neural relevance module using Criminal-BERT; the framework is model-agnostic and also compatible with BERT and Lawformer. [1]

用途：Base encoder [1]

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

来源：`raw/papers/b4a986af06bb-Uncertainty-aware-evidential-learning-for-legal-case-retrieval-with-noisy-correspondence.pdf` · 第 1 页 · 第 1 段 · `page:1#chars=0-680` · 来源摘录（未定位到主题原词，不代表逐条论断已核验）

> Information Sciences 702 (2025) 121915
> Available online 28 January 2025
> 0020-0255/© 2025 Elsevier Inc. All rights are reserved, including those for text and data mining, AI training, and similar technologies.
> Contents lists available at ScienceDirect
> Information Sciences
> journal homepage: www.elsevier.com/locate/ins
> Uncertainty-aware evidential learning for legal case retrieval with
> noisy correspondence ✩
> Weicong Qin a,
> , Weijie Yu b,
> ,∗, Kepu Zhang a,
> , Haiyuan Zhao a,
> , Jun Xu a,
> ,
> Ji-Rong Wen a,
> a Gaoling School of Art­ficial Intelligence, Renmin University of China, Beijing, 100872, Beijing, China
> b School of Information Technology and Management, University of Inte

## 我的笔记

<!-- user-notes:start -->

<!-- user-notes:end -->
