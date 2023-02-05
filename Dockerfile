# https://registry.suse.com/bci/rust/index.html
FROM registry.suse.com/bci/rust:1.66 AS build
WORKDIR /graph-do-smell

COPY Cargo.toml Cargo.lock .
# fetch --target to avoid fetching windows dependencies
RUN cargo fetch --locked --target x86_64-unknown-linux-gnu

COPY . .
RUN cargo build --locked --release --offline

# When /graph-do-smell/target is a bind mount from the host, it won't be
# available to copy --from in the next phase. Copy it somewhere relaible?
RUN mkdir /out && cp /graph-do-smell/target/release/graph-do-smell /out/graph-do-smell


# https://registry.suse.com/bci/bci-micro-15sp4/index.html
FROM registry.suse.com/bci/bci-micro:latest
COPY --from=build /out/graph-do-smell /usr/local/bin/graph-do-smell
ENTRYPOINT ["/usr/local/bin/graph-do-smell"]
