# Pre-publication task (end of the project, before the repo goes public)

1. Back up first: push a `backup/pre-rewrite` branch (and keep a local bundle).
2. Rewrite history: replace the "Day N …" commit messages with descriptive Conventional Commits, and change the author email of the first commit (GitHub's "Initial commit", personal Gmail) to `david@davidmichel.dev`. **Sign every commit of the rewritten history with SSH signing:**
   - **Setup:** `git config gpg.format ssh`, `user.signingkey <public key>`, `gpg.ssh.allowedSignersFile` (for local verification); add the public key to GitHub as a *signing* key.
   - **Sign:** re-sign after the message rewrite, e.g. `git rebase --root --gpg-sign --committer-date-is-author-date`.
   - **Check:** every commit shows `G` in `git log --format='%h %G? %s'`, and GitHub shows "Verified".
3. Move the tags `v0.1.0`, `v0.2.0`, … to the rewritten commits and update their draft releases. Recreate each tag as a **signed** annotated tag (`git tag -s`, same SSH key as the commits), check it with `git tag -v`, and push it with `--force` (tags only).
4. Replace "Day N" wording in the README and docs with version names (`v0.3.0` …).
5. Scan the full git history for secrets with gitleaks; fix anything found before publishing.
6. Review this file (CLAUDE.md) for internal details and trim it to what a public reader should see.
7. Add a `LICENSE` file with exactly this text, and link the README's copyright line to it:
   ```
   Copyright (c) 2026 David Michel. All rights reserved.

   You may view and fork this repository on GitHub for evaluation. You may not deploy, host, publish or present this work, or a derivative of it, as your own.
   ```
8. Delete this file (`docs/pre-publication.md`) and the "Before publication" pointer to it in CLAUDE.md.
