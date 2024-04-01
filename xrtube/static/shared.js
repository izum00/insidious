const lang = navigator.language
const thinNumberFormatter = new Intl.NumberFormat(lang , {notation: "compact"})
const numberFormatter = new Intl.NumberFormat(lang)
const yearsAgoFormatter = new Intl.DateTimeFormat(lang, {year: "numeric"})
const thisYearFormatter = new Intl.DateTimeFormat(lang, {
    month: "short", year: "numeric",
})
const thisMonthFormatter = new Intl.DateTimeFormat(lang, {
    day: "numeric", month: "short", year: "numeric",
})
const todayFormatter = new Intl.DateTimeFormat(lang, {
    hour: "numeric", minute: "numeric",
})
const futureFormatter = new Intl.DateTimeFormat(lang, {
    month: "short", day: "numeric", hour: "numeric", minute: "numeric",
})
const relativeFormatter = new Intl.RelativeTimeFormat(lang, {
    numeric: "auto", style: "short",
})
const normalDateFormatter = thisMonthFormatter

function formatYoutubeDate(timestamp) {
    const date = new Date(timestamp * 1000)
    const secondsAgo = (new Date() - date) / 1000
    const minutesAgo = Math.floor(secondsAgo / 60)
    const hoursAgo = Math.floor(minutesAgo / 60)
    const daysAgo = Math.floor(hoursAgo / 24)

    if (daysAgo >= 365)
        return yearsAgoFormatter.format(date)
    if (daysAgo >= 31)
        return thisYearFormatter.format(date)
    if (daysAgo >= 3)
        return thisMonthFormatter.format(date)
    if (daysAgo >= 1)
        return relativeFormatter.format(-daysAgo, "day")
    if (hoursAgo >= 1)
        return relativeFormatter.format(-hoursAgo, "hour")
    if (minutesAgo >= 1)
        return relativeFormatter.format(-minutesAgo, "minute")
    if (secondsAgo >= 0)
        return relativeFormatter.format(-secondsAgo, "second")
    if (daysAgo >= -1)
        return todayFormatter.format(date)
    return futureFormatter.format(date)
}

function formatYoutubeDayDate(timestamp) {
    return normalDateFormatter.format(new Date(timestamp * 1000))
}

function processText(selector, func) {
    document.querySelectorAll(selector).forEach(e => {
        e.innerText = (e.attributes.prefix?.value || "") +
            func(parseInt(e.attributes.raw.value, 10)) +
            (e.attributes.suffix?.value || "")
    })
}

function processAllText() {
    processText(".compact-number", thinNumberFormatter.format)
    processText(".number", numberFormatter.format)
    processText(".youtube-date", formatYoutubeDate)
    processText(".youtube-day-date", formatYoutubeDayDate)
}

function runHoverSlideshow(entry) {
    const thumbnails = entry.querySelector(".hover-thumbnails")
    const imgs = thumbnails.querySelectorAll("img")
    if (! imgs) return
    const current = thumbnails.querySelector("img.current")
    const next = thumbnails.querySelector("img.current + img") || imgs[0]

    function change() {
        current?.classList.remove("current")
        next.classList.add("current")
        if (! entry.matches(":hover")) {
            stopHoverSlideshow(entry)
            return
        }
        thumbnails.setAttribute("timer-id", 
            setTimeout(() => { runHoverSlideshow(entry) }, 1000) 
        )
    }

    if (next.hasAttribute("to-load")) {
        next.srcset = next.getAttribute("to-load")
        next.removeAttribute("to-load")
    }
    next.complete ? change() : next.addEventListener("load", change)
}

function stopHoverSlideshow(entry) {
    const thumbnails = entry.querySelector(".hover-thumbnails")
    clearTimeout(thumbnails.getAttribute("timer-id"))
    thumbnails.removeAttribute("timer-id")
    thumbnails.querySelector(".current")?.classList.remove("current")
}

function setCookie(name, obj, secondsAlive=0) {  // 0 = die on browser close
    const body = encodeURIComponent(JSON.stringify(obj))
    const age = secondsAlive ? `; max-age=${secondsAlive}` : ""
    document.cookie = `${name}=${body}; path=/; samesite=lax` + age
}

function setToughCookie(name, obj) {
    setCookie(name, obj, 60 * 60 * 24 * 400)  // max age of 400 days
}

function getCookie(name, default_=null) {
    for (const cookie of document.cookie.split("; "))
        if (cookie.split("=")[0] === name)
            return JSON.parse(decodeURIComponent(cookie.split("=")[1]))
    return default_
}
