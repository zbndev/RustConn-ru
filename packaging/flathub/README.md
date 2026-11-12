# RustConnRu Flatpak reference template

This directory is a local reference copy, not a Flathub submission. RustConnRu
is not published to Flathub, and this fork has no Flathub publication workflow
or token. Do not follow upstream's former automated-submission instructions.

The template is `io.github.zbndev.RustConnRu.yml`; its application module builds
this local checkout. The executable and gettext domain remain `rustconn`.

For supported downloadable Flatpak builds, use the manifests in
[`../flatpak/`](../flatpak/) and the manual release process in
[`../../docs/CI_BUILD_FLOW.md`](../../docs/CI_BUILD_FLOW.md). That workflow pins
the source to the exact reviewed commit of this fork.

Flathub's [contribution requirements](https://docs.flathub.org/docs/for-app-authors/requirements),
including its generative AI policy, apply to any actual submission. Updating
this local reference copy is not preparation or authorization to submit it.
