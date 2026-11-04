# Public landing page

This Terraform root manages Firebase Hosting for the public Revel landing page. Its local state is separate from the Cloud Run stack in the sibling terraform directory. Run from the repository root with Google Cloud Application Default Credentials.

```sh
terraform -chdir=infra/firebase init
terraform -chdir=infra/firebase plan
terraform -chdir=infra/firebase output -json required_dns_updates
firebase deploy --only hosting --project revel-gifts --config landing/firebase.json
```

Use the existing state at infra/firebase/terraform.tfstate in the primary checkout. When using a worktree, initialize with `-backend-config=path=/absolute/path/to/infra/firebase/terraform.tfstate` so the same state is used. Keep the state private and backed up. The deploy command publishes only the static site.

Add Firebase’s requested DNS records in Spaceship. The root domain serves the public site, and www redirects to it. Check both over HTTPS after the certificates become active. Sign-in links use the existing app host.
