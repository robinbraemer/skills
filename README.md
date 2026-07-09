# skills

Public Agent Skills shared by Robin Braemer.

## Skills

- [`quota-aware-model-selection`](quota-aware-model-selection/SKILL.md) — guidance for choosing between models, providers, accounts, quota pools, or backends using quota data.

  Useful with quota tools such as [`quota-axi`](https://github.com/kunchenguid/quota-axi). It keeps model fit first and quota second, while still capturing practical multi-account wins: for equivalent Codex accounts, spend quota that is most urgent before reset, otherwise drain the account with the least weekly quota remaining. It also covers explicitly approved early-use/primer hits for full accounts when that provider/window is confirmed to start its weekly reset clock on first use. The outcome is overlapping reset clocks and less wasted quota: a fuller account may still have nearly all quota left while already being days closer to reset.

  ![Quota-aware model selection timeline](assets/quota-aware-model-selection/quota-aware-model-selection.png)

  ![Overlapping reset windows timeline](assets/quota-aware-model-selection/overlapping-reset-windows.png)

## Usage

Copy or install the skill directory with an Agent Skills compatible harness.
