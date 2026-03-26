# Security Policy

## Supported Versions

We release patches for security vulnerabilities in the following versions:

| Version | Supported          |
| ------- | ------------------ |
| 0.x.x   | :white_check_mark: |

## Reporting a Vulnerability

We take the security of seekr-chain seriously. If you discover a security vulnerability, please follow these steps:

### For Security Issues

**Please do not open a public GitHub issue for security vulnerabilities.**

Instead, please report security vulnerabilities by emailing:

**lgrado@seekr.com**

Please include the following information in your report:

- Description of the vulnerability
- Steps to reproduce the issue
- Potential impact
- Suggested fix (if any)

### What to Expect

- **Acknowledgment**: We will acknowledge your email within 48 hours
- **Updates**: We will keep you informed about our progress in addressing the vulnerability
- **Disclosure**: Once we have addressed the vulnerability, we will work with you on an appropriate disclosure timeline
- **Credit**: We will credit you in the security advisory (unless you prefer to remain anonymous)

### Security Best Practices

When using seekr-chain, we recommend:

- **Secrets Management**: Never commit secrets or credentials to your repository. Use Kubernetes secrets or external secret management systems.
- **Image Security**: Use trusted container images and scan them for vulnerabilities.
- **Network Policies**: Configure appropriate network policies in your Kubernetes cluster.
- **RBAC**: Follow the principle of least privilege when configuring Kubernetes RBAC for Argo Workflows.
- **Updates**: Keep seekr-chain and its dependencies up to date.

## Security Update Process

When a security vulnerability is reported and confirmed:

1. We will develop a fix in a private repository
2. We will prepare a security advisory
3. We will release a patched version
4. We will publish the security advisory with details and mitigation steps

## Known Security Considerations

### S3 Access

Seekr-chain requires S3 credentials for code upload functionality. These credentials are:
- Stored as Kubernetes secrets
- Automatically cleaned up after 7 days
- Should have minimal permissions (read/write to specific bucket only)

### Kubernetes Access

Seekr-chain requires Kubernetes API access to:
- Create workflows and jobs
- Create and manage secrets
- Query pod status

Ensure appropriate RBAC policies are in place.

## Additional Resources

- [Contributing Guidelines](docs/developer/contributing.md)
- [Documentation](https://seekr-technologies.github.io/seekr-chain)
