/**
 * notifySlack.groovy — Send build/deploy notifications to Slack.
 */
def call(Map config) {
    def status  = config.status  // 'success' | 'failure'
    def service = config.service ?: env.SERVICE_NAME ?: 'unknown'
    def version = config.version ?: env.SERVICE_VERSION ?: '?'
    def branch  = env.BRANCH_NAME ?: 'unknown'
    def color   = status == 'success' ? '#36a64f' : '#e01e5a'
    def icon    = status == 'success' ? ':white_check_mark:' : ':x:'
    def msg     = "${icon} *${service}* `${version}` — ${branch} — ${status.toUpperCase()}"

    withCredentials([string(credentialsId: 'slack-webhook', variable: 'SLACK_WEBHOOK')]) {
        sh """
            curl -s -X POST -H 'Content-type: application/json' \
                --data '{"attachments":[{"color":"${color}","text":"${msg}","footer":"Jenkins • ${env.BUILD_URL}"}]}' \
                \$SLACK_WEBHOOK
        """
    }
}
