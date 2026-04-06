/**
 * generateReleaseNotes.groovy
 * Generates a GitHub Release from Conventional Commits since the previous tag.
 */
def call(Map config) {
    def service = config.service
    def version = config.version
    withCredentials([string(credentialsId: 'github-token', variable: 'GH_TOKEN')]) {
        sh """
            set -e
            PREV_TAG=\$(git describe --tags --abbrev=0 HEAD^ 2>/dev/null || echo "")
            if [ -z "\$PREV_TAG" ]; then
                RANGE="HEAD"
            else
                RANGE="\${PREV_TAG}..HEAD"
            fi

            NOTES=\$(git log \$RANGE --pretty=format:'%s' | awk '
                /^feat/          { sub(/^feat[^:]*: /, ""); print "- **New:** " \$0 }
                /^fix/           { sub(/^fix[^:]*: /,  ""); print "- **Fix:** " \$0 }
                /^perf/          { sub(/^perf[^:]*: /, ""); print "- **Perf:** " \$0 }
                /^BREAKING/      { sub(/^BREAKING CHANGE: /, ""); print "- **BREAKING:** " \$0 }
            ')

            IMAGE="${env.REGISTRY}/${service}:${version}"
            COMMIT=\$(git rev-parse --short HEAD)
            DATE=\$(date -u +%Y-%m-%d)

            BODY="## ${version} — \$DATE

\$NOTES

---
**Image:** \\\`\$IMAGE\\\`  
**Commit:** \\\`\$COMMIT\\\`"

            gh release create "v${version}" \
                --title "${service} v${version}" \
                --notes "\$BODY" \
                --repo "${env.GITHUB_ORG}/${service}"
        """
    }
}
