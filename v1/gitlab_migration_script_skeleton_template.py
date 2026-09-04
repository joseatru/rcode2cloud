
#!/usr/bin/env python3
"""
GitLab project migration: issues + notes + links + boards, preserving authorship using per-user PATs.

Requires:
  pip install python-gitlab

Inputs:
  config.yml:
    source:
      url: "https://onprem.gitlab.local"
      token: "SOURCE_ADMIN_TOKEN"
      project_path: "group/scicomp-kanban"
    dest:
      url: "https://gitlab.com"
      token: "DEST_MAINTAINER_TOKEN"  # used for setup/labels/boards
      namespace_id: 123456
      project_name: "scicomp-kanban"
    user_map:
      # source_username -> {dest_username, dest_pat}
      jbanfelder: { dest_username: "jason.banfelder", dest_pat: "..." }
      ...
"""

import gitlab, yaml, json, time
from pathlib import Path

STATE = Path(".state")
STATE.mkdir(exist_ok=True)

def load_cfg():
    return yaml.safe_load(Path("config.yml").read_text())

def gl_client(url, token):
    gl = gitlab.Gitlab(url, private_token=token)
    gl.auth()
    return gl

def checkpoint(name, data):
    (STATE / f"{name}.json").write_text(json.dumps(data, indent=2))

def load_checkpoint(name, default=None):
    p = STATE / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else default

def export_source(gls, project):
    # Export issues, notes, links, boards into local files (JSONL)
    # Use pagination get_all=True for python-gitlab
    pass

def ensure_dest_project(gld, cfg):
    # Create project if missing
    pass

def migrate_labels_milestones(gls_proj, gld_proj, gld):
    pass

def migrate_issues(gls_proj, gld_proj, cfg):
    """
    Create issues in IID order.
    Use author token for creating issue (so author is correct).
    Then set assignees using dest maintainer token if needed.
    """
    pass

def migrate_notes(gls_proj, gld_proj, cfg, iid_map):
    """
    For each issue IID, create notes using correct author's PAT.
    Notes API supports created_at when creating issue note (optional). [11](https://docs.gitlab.com/api/notes/)
    """
    pass

def migrate_links(gls_proj, gld_proj, gld, iid_map):
    """
    Use Issue Links API to recreate relationships after all issues exist. [12](https://docs.gitlab.com/api/issue_links/)
    """
    pass

def migrate_boards(gls_proj, gld_proj, gld):
    """
    Use Boards API to recreate boards/lists. [13](https://docs.gitlab.com/api/boards/)
    """
    pass

def main():
    cfg = load_cfg()
    gls = gl_client(cfg["source"]["url"], cfg["source"]["token"])
    gld = gl_client(cfg["dest"]["url"], cfg["dest"]["token"])

    src_proj = gls.projects.get(cfg["source"]["project_path"])
    dst_proj = ensure_dest_project(gld, cfg)

    # 1) export_source(gls, src_proj)
    # 2) migrate_labels_milestones(src_proj, dst_proj, gld)
    # 3) iid_map = migrate_issues(src_proj, dst_proj, cfg)
    # 4) migrate_notes(src_proj, dst_proj, cfg, iid_map)
    # 5) migrate_links(src_proj, dst_proj, gld, iid_map)
    # 6) migrate_boards(src_proj, dst_proj, gld)

if __name__ == "__main__":
    main()
