let remainingTries = 0

function recheckDaemonStatus () {
  remainingTries = 10

  if (document.getElementById('status').innerHTML !== 'Running') {
    checkDaemonStatus()
  }
}

function checkDaemonStatus () {
  window.fetch('/ipfsdaemon/status')
    .then(resp => updateUIOrResetTimer())
    .catch(error => console.log('error on daemon status check', error))
}

function updateUIOrResetTimer (resp) {
  if (remainingTries > 0) { // Stop polling after 10 sec/tries
    remainingTries -= 1
  }
  if (resp.indexOf('Not Running') > -1) {
    window.setTimeout(checkDaemonStatus, 1000)
  } else {
    document.location.reload(true)
  }
}

document.addEventListener('DOMContentLoaded', recheckDaemonStatus, false)
