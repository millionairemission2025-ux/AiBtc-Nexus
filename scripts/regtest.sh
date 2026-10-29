#!/usr/bin/env bash
# Start a throwaway Lightning regtest network: btcd + two LND nodes (alice buys,
# bob sells) with a funded channel alice -> bob, then print the environment for
# tests/test_regtest_lnd.py.
#
#   scripts/regtest.sh up      # download (checksum-verified), start, fund, open channel
#   source "$REGTEST_DIR/env.sh"
#   pytest tests/test_regtest_lnd.py -v
#   scripts/regtest.sh down    # stop everything
#
# Nothing here touches real bitcoin. Linux x86_64 only.
set -euo pipefail

LND_VERSION=v0.18.3-beta
BTCD_VERSION=v0.24.2
R=${REGTEST_DIR:-/tmp/aibtc-nexus-regtest}

btcctl() { "$R/bin/btcctl" --regtest --rpcuser=u --rpcpass=p --rpccert="$R/btcd/rpc.cert" \
  --rpcserver=127.0.0.1:18334 "$@"; }
lncli() {
  local node=$1; shift
  local port; port=$([ "$node" = alice ] && echo 10009 || echo 10010)
  "$R/bin/lncli" --lnddir="$R/$node" --network=regtest --rpcserver=127.0.0.1:"$port" "$@"
}
json() { python3 -c "import sys,json;print(json.load(sys.stdin)$1)"; }

stop_pidfile() {  # graceful stop, then force after 15 s (btcd can ignore SIGTERM)
  local file=$1 pid
  [ -f "$file" ] || return 0
  pid=$(cat "$file")
  kill "$pid" 2>/dev/null || { rm -f "$file"; return 0; }
  for _ in $(seq 1 15); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
  kill -9 "$pid" 2>/dev/null || true
  for _ in $(seq 1 5); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
  rm -f "$file"
}

fetch() {  # fetch NAME URL MANIFEST_URL
  local file=$1 url=$2 manifest=$3
  curl -sSfL -o "$R/dl/$file" "$url"
  curl -sSfL -o "$R/dl/$file.manifest" "$manifest"
  local want; want=$(grep " $(basename "$url")\$" "$R/dl/$file.manifest" | cut -d' ' -f1)
  local got; got=$(sha256sum "$R/dl/$file" | cut -d' ' -f1)
  [ -n "$want" ] && [ "$want" = "$got" ] || { echo "checksum mismatch for $file" >&2; exit 1; }
}

install() {
  [ -x "$R/bin/lnd" ] && [ -x "$R/bin/btcd" ] && return
  mkdir -p "$R/dl" "$R/bin"
  fetch lnd.tar.gz \
    "https://github.com/lightningnetwork/lnd/releases/download/$LND_VERSION/lnd-linux-amd64-$LND_VERSION.tar.gz" \
    "https://github.com/lightningnetwork/lnd/releases/download/$LND_VERSION/manifest-$LND_VERSION.txt"
  fetch btcd.tar.gz \
    "https://github.com/btcsuite/btcd/releases/download/$BTCD_VERSION/btcd-linux-amd64-$BTCD_VERSION.tar.gz" \
    "https://github.com/btcsuite/btcd/releases/download/$BTCD_VERSION/manifest-$BTCD_VERSION.txt"
  tar -xzf "$R/dl/lnd.tar.gz" -C "$R/dl" 2>/dev/null
  tar -xzf "$R/dl/btcd.tar.gz" -C "$R/dl" 2>/dev/null
  cp "$R"/dl/lnd-linux-amd64-*/lnd "$R"/dl/lnd-linux-amd64-*/lncli "$R/bin/"
  cp "$R"/dl/btcd-linux-amd64-*/btcd "$R"/dl/btcd-linux-amd64-*/btcctl "$R/bin/"
}

start_btcd() {
  mkdir -p "$R/btcd"
  nohup "$R/bin/btcd" --regtest --datadir="$R/btcd/data" --logdir="$R/btcd/logs" -C /dev/null \
    --rpccert="$R/btcd/rpc.cert" --rpckey="$R/btcd/rpc.key" --rpcuser=u --rpcpass=p \
    --rpclisten=127.0.0.1:18334 --listen=127.0.0.1:18444 --txindex "$@" \
    > "$R/btcd/out.log" 2>&1 &
  echo $! > "$R/btcd.pid"
  for _ in $(seq 1 30); do btcctl getblockcount >/dev/null 2>&1 && return; sleep 1; done
  echo "btcd did not start" >&2; exit 1
}

start_lnd() {  # NAME P2P RPC REST
  nohup "$R/bin/lnd" --lnddir="$R/$1" --alias="$1" --noseedbackup \
    --bitcoin.regtest --bitcoin.node=btcd --btcd.rpchost=127.0.0.1:18334 \
    --btcd.rpcuser=u --btcd.rpcpass=p --btcd.rpccert="$R/btcd/rpc.cert" \
    --listen=127.0.0.1:"$2" --rpclisten=127.0.0.1:"$3" --restlisten=127.0.0.1:"$4" \
    --bitcoin.defaultchanconfs=1 > "$R/$1.log" 2>&1 &
  echo $! > "$R/$1.pid"
}

wait_synced() {
  for _ in $(seq 1 60); do
    if lncli "$1" getinfo 2>/dev/null | grep -q '"synced_to_chain": *true'; then return; fi
    sleep 1
  done
  echo "$1 did not sync" >&2; exit 1
}

up() {
  install
  start_btcd
  start_lnd alice 19735 10009 8081
  start_lnd bob 19736 10010 8082
  for _ in $(seq 1 60); do lncli alice getinfo >/dev/null 2>&1 && break; sleep 1; done
  for _ in $(seq 1 60); do lncli bob getinfo >/dev/null 2>&1 && break; sleep 1; done

  # btcd needs the mining address at startup; 500 blocks also activate segwit.
  local addr; addr=$(lncli alice newaddress p2wkh | json "['address']")
  stop_pidfile "$R/btcd.pid"  # the old process must release its ports first
  start_btcd --miningaddr="$addr"
  btcctl generate 500 > /dev/null
  wait_synced alice; wait_synced bob

  local bob; bob=$(lncli bob getinfo | json "['identity_pubkey']")
  # getinfo answers before the RPC server is fully started: retry until it is.
  local connected=""
  for _ in $(seq 1 60); do
    if lncli alice connect "$bob@127.0.0.1:19736" > /dev/null 2>&1 \
      || lncli alice listpeers | grep -q "$bob"; then
      connected=1; break
    fi
    sleep 1
  done
  [ -n "$connected" ] || { echo "alice could not connect to bob" >&2; exit 1; }
  for _ in $(seq 1 30); do
    lncli alice openchannel --node_key "$bob" --local_amt 2000000 --push_amt 50000 \
      > /dev/null 2>&1 && break
    sleep 2
  done
  btcctl generate 6 > /dev/null
  for _ in $(seq 1 60); do
    if lncli alice listchannels | grep -q '"active": *true'; then break; fi
    sleep 1; btcctl generate 1 > /dev/null
  done
  lncli alice listchannels | grep -q '"active": *true' || { echo "channel not active" >&2; exit 1; }
  # "active" can precede the channel being usable: wait until a probe payment works.
  local paid=""
  for _ in $(seq 1 60); do
    local pr; pr=$(lncli bob addinvoice --amt 1 | json "['payment_request']")
    if lncli alice payinvoice --force "$pr" 2>/dev/null | grep -q SUCCEEDED; then
      paid=1; break
    fi
    sleep 1
  done
  [ -n "$paid" ] || { echo "channel never became usable" >&2; exit 1; }

  hex() { python3 -c "import sys;print(open(sys.argv[1],'rb').read().hex())" "$1"; }
  cat > "$R/env.sh" <<EOF
export NEXUS_IT_BUYER_LND_URL=https://127.0.0.1:8081
export NEXUS_IT_BUYER_MACAROON_HEX=$(hex "$R/alice/data/chain/bitcoin/regtest/admin.macaroon")
export NEXUS_IT_BUYER_TLS_CERT=$R/alice/tls.cert
export NEXUS_IT_PROVIDER_LND_URL=https://127.0.0.1:8082
export NEXUS_IT_PROVIDER_MACAROON_HEX=$(hex "$R/bob/data/chain/bitcoin/regtest/admin.macaroon")
export NEXUS_IT_PROVIDER_TLS_CERT=$R/bob/tls.cert
EOF
  echo "regtest network ready: source $R/env.sh"
}

down() {
  for name in alice bob btcd; do stop_pidfile "$R/$name.pid"; done
  rm -rf "$R/alice" "$R/bob" "$R/btcd" "$R"/*.pid "$R/env.sh"
  echo "stopped"
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  *) echo "usage: $0 up|down" >&2; exit 2 ;;
esac
