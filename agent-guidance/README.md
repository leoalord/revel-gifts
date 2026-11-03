# Agent guidance

The canonical, versioned agent guidance lives as package data in
[`backend/src/revel/guidance/skills/`](../backend/src/revel/guidance/skills/):
`<name>/SKILL.md` per skill, plus `index.md` and `CHANGELOG.md`. Each file has front matter
(`name`, `description`, `version` as semver, `status`). Edit those files only; do not keep copies
here. Bump `version` and add a `CHANGELOG.md` entry for every content change.

One renderer (`revel.guidance.render_markdown`) produces both delivery formats from the same
bytes:

- website Markdown: `GET /guidance` (index) and `GET /guidance/<name>.md`, public, `text/markdown`;
- MCP: each skill as a no-argument prompt named after the skill, and each document as the
  resource `revel://guidance/<name>` (`text/markdown`).

The skills are `revel-manage-profile`, `revel-connect-and-read`, `revel-find-a-gift` and
`revel-answer-question`. A renamed skill is listed in `RENAMED_SKILLS` in
`revel.guidance.operations`: its old website page permanently redirects (`308`) to the new one,
its old MCP prompt and resource are removed, and `backend/tests/guidance/test_retired_tools.py`
fails if served guidance still names it. Since 0.7.0, `revel-profile-discovery` is
`revel-manage-profile` and `revel-giver-workflow` is `revel-find-a-gift`.

Operation names in the guidance are checked by `backend/tests/guidance/test_content_lint.py`
against `revel.guidance.operations`: anything not live must be marked `<!-- PROVISIONAL ... -->`
on the same line. Move a name to the implemented set only once its contract is merged and
deployed.

[Agent-Led Onboarding](https://app.notion.com/p/40a2bade7f3181eab40de24dfbe88adb) owns guidance requirements and client verification. [Privacy](https://app.notion.com/p/40b2bade7f31811d8e55d8818e949b31) owns contribution rules; [Questions](https://app.notion.com/p/40a2bade7f3181bf9975f7fa9a1f597d) owns routing and lifecycle. Runtime instructions must implement those decisions; this README is not a separate policy specification. Document only tested client capabilities and supported operations.
