/* eslint-env serviceworker */
/* global Reconstructive */

// This makes a class module available named "Reconstructive"
importScripts('/ipwbassets/reconstructive.js')

// Create a Reconstructive instance with optionally customized configurations
// const rc = new Reconstructive({
//   id: `${NAME}:${VERSION}`,
//   urimPattern: `${self.location.origin}/memento/<datetime>/<urir>`,
//   bannerElementLocation: `${self.location.origin}/reconstructive-banner.js`,
//   bannerLogoLocation: '',
//   bannerLogoHref: '/',
//   showBanner: false,
//   debug: false
// });
const rc = new Reconstructive({
  bannerElementLocation: '/ipwbassets/reconstructive-banner.js',
  showBanner: true,
  bannerLogoLocation: '/ipwbassets/logo.png',
  debug: true
})

// Add any custom exclusions or modify or delete default ones
// > rc.exclusions;
// < {
// <   notGet: function(FetchEvent) => boolean,
// <   bannerElement: function(FetchEvent) => boolean,
// <   bannerLogo: function(FetchEvent) => boolean,
// <   localResource: function(FetchEvent) => boolean
// < }
rc.exclusions.specialEndpint = function (event, config) {
  return ['/ipwbassets/', '/ipfsdaemon/'].some(
    ep => event.request.url.startsWith(self.location.origin + ep))
}

// This is not necessary, but can be useful for debugging or in future
self.addEventListener('install', (event) => {
  console.log('ServiceWorker installed')
})

// This is not necessary, but can be useful for debugging or in future
self.addEventListener('activate', (event) => {
  console.log('ServiceWorker Activated')
})

self.addEventListener('fetch', (event) => {
  // Add any custom logic here to conditionally call the reroute method
  rc.reroute(event)
})
