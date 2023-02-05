#!/usr/bin/bash
set -e
token=$(curl -X POST \
        -H 'authorization: bearer XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX' \
        -H 'accept: application/vnd.github+json' \
        https://api.github.com/repos/sqwishy/graph-do-smell/actions/runners/registration-token \
        | jq -r .token)
install <(echo -e "#!/usr/bin/bash\necho $token | ./config remove") ./unconfig.sh
./config.sh $@ --url https://github.com/sqwishy/graph-do-smell --token $token
