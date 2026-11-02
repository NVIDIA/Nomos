// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

// Static local evidence only: no HTTP calls, storage, analytics or external libraries.
const evidence = JSON.parse(document.getElementById("downloads").textContent);
const category = document.getElementById("category");
const cohort = document.getElementById("cohort");
const onlyErrors = document.getElementById("only-errors");
function filterCases() {
  let visible = 0;
  document.querySelectorAll("details[data-case]").forEach((element) => {
    element.hidden =
      (category.value !== "all" && element.dataset.category !== category.value) ||
      (cohort.value !== "all" && element.dataset.cohort !== cohort.value) ||
      (onlyErrors.checked && element.dataset.error !== "true");
    if (!element.hidden) visible++;
  });
  document.getElementById("visible-count").textContent = `${visible} of 96 cases shown.`;
}
[category, cohort, onlyErrors].forEach((element) => element.addEventListener("change", filterCases));
filterCases();
document.querySelectorAll("button[data-sha]").forEach((button) => {
  button.addEventListener("click", () => {
    const value = evidence[button.dataset.sha][button.dataset.kind];
    const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2) + "\n"], {type: "application/json"}));
    const link = document.createElement("a");
    link.href = url;
    link.download = `${button.dataset.kind}-${button.dataset.sha}.json`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
});
