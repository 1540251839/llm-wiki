---
type: paper
title: "Uncertainty-aware evidential learning for legal case retrieval with noisy correspondence"
authors: "Weicong Qin, Weijie Yu, Kepu Zhang, Haiyuan Zhao, Jun Xu, Ji-Rong Wen"
year: 2025
source: "raw/papers/b4a986af06bb-Uncertainty-aware-evidential-learning-for-legal-case-retrieval-with-noisy-correspondence.pdf"
updated_at: "2026-09-19T01:58:19+00:00"
---

# Uncertainty-aware evidential learning for legal case retrieval with noisy correspondence

## 基本信息

作者：Weicong Qin, Weijie Yu, Kepu Zhang, Haiyuan Zhao, Jun Xu, Ji-Rong Wen

年份：2025

来源：`raw/papers/b4a986af06bb-Uncertainty-aware-evidential-learning-for-legal-case-retrieval-with-noisy-correspondence.pdf`

## 摘要

Legal case retrieval is a critical task in intelligent legal systems, providing relevant precedents to assist judges in their decision-making. While current data-driven neural retrieval methods have demonstrated impressive performance on clean, annotated data, they often ignore the robustness against noisy correspondences. In practice, legal annotators are required to identify legal uncertainty, which refers to the ambiguity or unpredictability in legal interpretations and applications, in relevance estimation between cases. This uncertainty often introduces noise into the training data, leading to unreliable predictions and potentially impacting the fairness and justice of downstream tasks. Focusing on this robustness issue, we propose a novel evidential learning framework called ELCR, which explicitly models the legal uncertainty and addresses noisy correspondences. Specifically, we first estimate the multi-faceted relevance between query-candidate cases from the concept, rule, and fact levels. These relevance estimations are then used to obtain the evidence-based uncertainty under the Dempster-Shafer Evidence Theory, which helps correct labels from noisy correspondence. Guided by two elaborate evidence-based training objectives, ELCR provides accurate uncertainty estimation, enhancing reliability and robustness. Extensive experiments on various noise proportions across two benchmark datasets demonstrate that our method exhibits robustness against noisy correspondences while maintaining competitive retrieval performance.

## 研究问题

Existing neural legal case retrieval methods rely on clean annotated data and overlook robustness against noisy correspondences caused by legal uncertainty (ambiguity at the concept, rule, and fact levels), leading to unreliable predictions and limiting practical application.

## 核心方法

Proposes ELCR, an evidential learning framework that estimates multi-faceted query-candidate relevance from concept, rule, and fact levels; parameterizes relevance evidence into a Dirichlet distribution under Dempster-Shafer Evidence Theory to quantify uncertainty; corrects noisy labels via an uncertainty-based strategy with warm-up stabilization; and optimizes with standard relevance loss plus two evidence-based objectives.

## 主要贡献

- First evidential learning framework (ELCR) to explicitly model multi-faceted relevance uncertainties and detect noisy correspondences in legal case retrieval.
- Uncertainty-based label correction strategy with warm-up and stabilization mechanisms to prevent over-correction during training.
- Two uncertainty-based learning objectives—evidence-based contrastive loss and uncertainty penalty loss—to ensure a desired evidence distribution.
- Demonstrated robustness against various noise proportions (10%–90%) while maintaining competitive retrieval performance.

## 方法

- [[Multi-faceted relevance modeling]]
- [[Evidential uncertainty estimation]]
- [[Label correction strategy]]
- [[Evidence-based contrastive learning]]
- [[Uncertainty penalty loss]]
- [[Criminal-BERT backbone]]

## 数据集

- [[LeCaRD]]
- [[ELAM]]

## 评价指标

- NDCG

## 主要实验结论

- ELCR exhibits robustness against noisy correspondences while maintaining competitive retrieval performance across benchmark datasets.
- Achieves average NDCG improvements of 20.90% on LeCaRD and 10.99% on ELAM compared to various baselines.
- Explicit modeling of legal uncertainty via evidential learning improves reliability and enables effective noise identification.
- Framework is adaptable across different backbone architectures, including BERT, Criminal-BERT, and Lawformer.

## 局限性

- In high-noise scenarios, imperfect differentiation between noisy positive samples and true negatives may reduce the number of available positive samples and degrade performance.
- Additional computational overhead from evidence calculation, uncertainty estimation, and label correction may challenge resource-constrained environments.

## 相关研究主题

- [[Legal case retrieval]]
- [[Evidential learning]]
- [[Noisy correspondence]]
- [[Legal uncertainty]]
- [[Dempster-Shafer theory]]
- [[Intelligent legal systems]]
- [[Information retrieval]]
- [[Label correction]]

## 来源

原始文件：`raw/papers/b4a986af06bb-Uncertainty-aware-evidential-learning-for-legal-case-retrieval-with-noisy-correspondence.pdf`

## 我的笔记

<!-- user-notes:start -->

<!-- user-notes:end -->
