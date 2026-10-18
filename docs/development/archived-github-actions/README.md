# Archived GitHub Actions workflows

GitHub Actions automation is intentionally disabled in the repository source
tree. The files in this directory are byte-for-byte snapshots of the former
workflow definitions. They use the `.yml.disabled` suffix and live outside
`.github/workflows`, so GitHub does not treat them as active workflows.

The workflows are archived because release management, promotion, and CI
policy have not yet been designed as one reviewed system.
In particular, the former image workflow scanned a separately built
`linux/amd64` image and later rebuilt and published a multi-architecture
manifest. Reusing a build cache did not prove that the published amd64 image
was byte-identical to the scanned image, and the arm64 image was not scanned
at all. The old CI definition also maintained parallel npm and pnpm jobs.

| Archived snapshot | Former active path | SHA-256 |
| --- | --- | --- |
| `ci.yml.disabled` | `.github/workflows/ci.yml` | `1c2789f26b65316c8fac2d132efc0f27aa0cd5e27afababd52e9958cd3973dc2` |
| `release-images.yml.disabled` | `.github/workflows/release-images.yml` | `6d7ef47243e77861fa63534c548973a9ca1dc0556cb290231d2842733ec59c85` |

These snapshots are audit evidence, not supported execution templates. Do not
copy or rename them back into `.github/workflows` without a fresh architecture
and security review covering triggers, permissions, immutable build inputs,
scanning of every publishable image digest, artifact promotion without a
second unverified build, release approval, and required checks.

This archive records only the source-controlled state. Repository-level GitHub
Actions settings, queued or running jobs, and historical workflow runs must be
checked and controlled separately by a repository administrator.
