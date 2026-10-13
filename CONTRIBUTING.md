# Contributing

Sign off each commit to confirm the
[Developer Certificate of Origin 1.1](https://developercertificate.org/):

```bash
git commit --signoff -m "fix: describe the change"
```

The DCO app checks commit sign-offs, including contributions from organization
members. Use the same name and email in the commit author and sign-off.

Your Git email address appears in public commits and sign-offs. Use GitHub's
provided noreply address [as your `user.email`](https://docs.github.com/en/account-and-profile/setting-up-and-managing-your-personal-account-on-github/managing-email-preferences/setting-your-commit-email-address#setting-your-commit-email-address-in-git),
[keep your email private](https://docs.github.com/en/account-and-profile/setting-up-and-managing-your-personal-account-on-github/managing-email-preferences/setting-your-commit-email-address#setting-your-commit-email-address-on-github),
and enable [blocking pushes that expose your personal email](https://docs.github.com/en/account-and-profile/setting-up-and-managing-your-personal-account-on-github/managing-email-preferences/blocking-command-line-pushes-that-expose-your-personal-email-address).

Use conventional commit messages and a conventional pull request title, such
as `feat: add workflow submission`, `fix: validate workflow inputs`, or
`chore: update repository policies`. The Semantic PR app validates both the
pull request title and commit messages.

DCO and Semantic PR are required merge checks. Pull requests also need the
configured owner review and approval of the latest push.

Report suspected vulnerabilities through the private channels in
[SECURITY.md](SECURITY.md).
