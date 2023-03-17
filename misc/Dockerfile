# This is meant to build an image to be used with systemd-nspawn/machinectl.
#
# https://froghat.ca/2023/03/github-runner-cache/#self-hosting-github-s-runner-in-systemd-nspawn

FROM opensuse/tumbleweed

# > tar gzip
# to unpack actions-runner
# > lttng-ust libopenssl1_1 krb5 zlib libicu
# because it's what bin/installdependencies.sh tries to install
# > systemd-network
# for systemd-networkd systemd-resolved; for the machine to behave when boot
# under systemd-nspawn
RUN zypper in -y --no-recommends tar gzip lttng-ust libopenssl1_1 krb5 zlib libicu systemd-network podman buildah jq ncat sudo

COPY ./github-runner.service /etc/systemd/system/github-runner.service

# This really feels like bad form -- the && stuff. I've seen things do this to
# avoid making layers. But it's not super clear to me why that's valuable. And,
# if it really is, shouldn't that be solved a different way? This kind of seems
# like a bit of a hack.
RUN useradd --create-home --user-group ghrunner \
 && echo ghrunner:65536:65536 | tee /etc/subuid /etc/subgid \
 && mkdir /home/ghrunner/cache \
 && echo 'ghrunner ALL=(root) NOPASSWD: /usr/bin/umount /home/ghrunner/cache' > /etc/sudoers.d/50_ghrunner-cache \
 && systemctl enable systemd-networkd systemd-resolved github-runner \
 # for some reason, newuidmap doesn't seem to have capabilities set correct in
 # the base image or something
 && rpm --setcaps shadow \
 # loginctl enable-linger ghrunner so it can do cgroup things?
 && mkdir /var/lib/systemd/linger \
 && touch /var/lib/systemd/linger/ghrunner

# This script is used by github-runner.service to configure the runner. It
# needs to get a token from GitHub's API and the token for bearer auth is
# expected to be written into this file. That's hardly ideal, but I didn't care
# to come up with something clever here because I'm lazy and what works for me
# probably won't work for you here anyway.
COPY ./get-token-and-config.sh /home/ghrunner/get-token-and-config.sh
RUN chmod a+rx /home/ghrunner/get-token-and-config.sh

USER ghrunner
WORKDIR /home/ghrunner
ARG RUNNER_DOWNLOAD_URL=https://github.com/actions/runner/releases/download/v2.302.1/actions-runner-linux-x64-2.302.1.tar.gz
RUN curl -L ${RUNNER_DOWNLOAD_URL} -o- \
  | tar --exclude 'externals/node12' \
        --exclude 'externals/node12_alpine' \
        --exclude 'externals/node16_alpine' \
        -xz
