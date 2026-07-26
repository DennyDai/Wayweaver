FROM ubuntu:26.04

ARG DEBIAN_FRONTEND=noninteractive
ARG TARGETARCH
ARG VM_UID=1000
ARG VM_GID=1000

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN if [[ "${TARGETARCH}" != "amd64" ]]; then \
      echo "Google Chrome for Linux requires an amd64 image (got ${TARGETARCH})" >&2; \
      exit 1; \
    fi \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      adwaita-icon-theme \
      at-spi2-core \
      build-essential \
      ca-certificates \
      curl \
      dbus-x11 \
      fonts-dejavu-core \
      fonts-liberation \
      git \
      jq \
      libatk-wrapper-java-jni \
      less \
      locales \
      maim \
      nano \
      novnc \
      openssh-server \
      procps \
      python3 \
      python3-pip \
      python3-pyatspi \
      python3-xlib \
      socat \
      sudo \
      supervisor \
      tesseract-ocr \
      thunar \
      tigervnc-standalone-server \
      tigervnc-tools \
      unzip \
      vim-tiny \
      websockify \
      wget \
      wmctrl \
      x11-utils \
      xauth \
      xclip \
      xdotool \
      xfce4-panel \
      xfce4-session \
      xfce4-settings \
      xfce4-terminal \
      xfdesktop4 \
      xfwm4 \
      zip \
 && curl -fsSL -o /tmp/google-chrome.deb \
      https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
 && apt-get install -y --no-install-recommends /tmp/google-chrome.deb \
 && rm -f /tmp/google-chrome.deb \
 && locale-gen en_US.UTF-8 \
 && rm -rf /var/lib/apt/lists/*

# Chrome's save-password bubble and first-run promos steal focus and cover the
# page mid-flow, which stalls automation on an unrelated modal. Policy disables
# them for every profile in the image.
RUN mkdir -p /etc/opt/chrome/policies/managed \
 && printf '%s\n' \
      '{' \
      '  "PasswordManagerEnabled": false,' \
      '  "AutofillAddressEnabled": false,' \
      '  "AutofillCreditCardEnabled": false,' \
      '  "PromotionalTabsEnabled": false,' \
      '  "DefaultBrowserSettingEnabled": false' \
      '}' \
      > /etc/opt/chrome/policies/managed/wayweaver.json

RUN groupmod --new-name vm ubuntu \
 && groupmod --gid "${VM_GID}" vm \
 && usermod --login vm --uid "${VM_UID}" --gid "${VM_GID}" --home /home/vm --move-home --shell /bin/bash ubuntu \
 && usermod -aG sudo vm \
 && printf 'vm ALL=(ALL) NOPASSWD:ALL\n' >/etc/sudoers.d/vm \
 && chmod 0440 /etc/sudoers.d/vm \
 && install -d -m 0755 /run/sshd \
 && printf '%s\n' \
      'HostKey /etc/ssh/wayweaver/ssh_host_ed25519_key' \
      'HostKey /etc/ssh/wayweaver/ssh_host_rsa_key' \
      'PasswordAuthentication yes' \
      'PermitRootLogin no' \
      'X11Forwarding no' \
      'AllowTcpForwarding yes' \
      >/etc/ssh/sshd_config.d/desktop-vm.conf

COPY docker/chrome /usr/local/bin/chrome
COPY docker/start-desktop /usr/local/bin/start-desktop
COPY docker/enable-accessibility /usr/local/bin/enable-accessibility
COPY docker/session-rejoin /usr/local/bin/session-rejoin
COPY docker/start-chrome /usr/local/bin/start-chrome
COPY src/wayweaver/_version.py /tmp/wayweaver-version.py
COPY src/wayweaver/runtime/assets/linux/ /tmp/wayweaver-runtime/
COPY docker/entrypoint /usr/local/bin/desktop-vm-entrypoint
COPY docker/supervisord.conf /etc/supervisor/conf.d/desktop-vm.conf
COPY docker/desktop-env.sh /etc/profile.d/desktop-env.sh

RUN version="$(python3 -c 'scope={}; exec(open("/tmp/wayweaver-version.py").read(), scope); print(scope["__version__"])')" \
 && runtime="/opt/wayweaver/runtime/${version}/bin" \
 && install -d -m 0755 "${runtime}" \
 && install -m 0755 /tmp/wayweaver-runtime/wayweaver-applications "${runtime}/wayweaver-applications" \
 && install -m 0755 /tmp/wayweaver-runtime/wayweaver-atspi "${runtime}/wayweaver-atspi" \
 && install -m 0755 /tmp/wayweaver-runtime/wayweaver-clipboard "${runtime}/wayweaver-clipboard" \
 && install -m 0755 /tmp/wayweaver-runtime/wayweaver-x11-record "${runtime}/wayweaver-x11-record" \
 && ln -s "${runtime}/wayweaver-applications" /usr/local/bin/wayweaver-applications \
 && ln -s "${runtime}/wayweaver-atspi" /usr/local/bin/wayweaver-atspi \
 && ln -s "${runtime}/wayweaver-clipboard" /usr/local/bin/wayweaver-clipboard \
 && ln -s "${runtime}/wayweaver-x11-record" /usr/local/bin/wayweaver-x11-record \
 && rm -rf /tmp/wayweaver-runtime /tmp/wayweaver-version.py \
 && chmod 0755 \
      /usr/local/bin/chrome \
      /usr/local/bin/start-desktop \
      /usr/local/bin/enable-accessibility \
      /usr/local/bin/session-rejoin \
      /usr/local/bin/start-chrome \
      /usr/local/bin/desktop-vm-entrypoint \
      /etc/profile.d/desktop-env.sh

ENV LANG=en_US.UTF-8 \
    LANGUAGE=en_US:en \
    LC_ALL=en_US.UTF-8

EXPOSE 22 5901 6080 9222

HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=6 \
  CMD curl -fsS http://127.0.0.1:9222/json/version >/dev/null \
   && bash -c '</dev/tcp/127.0.0.1/22' \
   && bash -c '</dev/tcp/127.0.0.1/6080' \
   || exit 1

ENTRYPOINT ["/usr/local/bin/desktop-vm-entrypoint"]
