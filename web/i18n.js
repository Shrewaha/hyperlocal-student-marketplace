/*
 * English and Hindi for the interface.
 *
 * The shops this is for are kirana and electronics counters in Ayodhya. Their
 * owners read Devanagari far more comfortably than English, and a catalogue
 * form labelled in a language you are guessing at is a form you fill in
 * slowly and wrongly. This is not decoration; it is the difference between a
 * shopkeeper listing thirty items and giving up after two.
 *
 * Kept deliberately small: the screens a shop actually works in, the
 * navigation, and the words on buttons that do something. Student-facing
 * prose is left in English for now rather than half-translated.
 *
 * Product categories stay bilingual in one string ("Electronics /
 * इलेक्ट्रॉनिक्स") because the server stores the English name and a shop
 * needs to recognise the row either way.
 */

window.STRINGS = {
  en: {
    "nav.browse": "Browse",
    "nav.post": "Post",
    "nav.requests": "Requests",
    "nav.offers": "Offers",
    "nav.catalogue": "My shop",
    "nav.chats": "Chats",
    "nav.account": "Account",
    "nav.signin": "Sign in",

    "cat.title": "My shop",
    "cat.sub": "Add what you sell. Students searching nearby will find it.",
    "cat.add": "Add an item",
    "cat.empty.title": "Nothing listed yet.",
    "cat.empty.body": "Add your first item. It takes a name and a price.",
    "cat.count.one": "1 item",
    "cat.count.many": "{n} items",

    "add.title": "Add an item",
    "add.edit": "Edit item",
    "add.photo": "Add a photo",
    "add.retake": "Change photo",
    "add.name": "What is it?",
    "add.name.ph": "ESP8266 board",
    "add.price": "Price",
    "add.category": "Type",
    "add.more": "More details",
    "add.specs": "Details",
    "add.stock": "How many",
    "add.save": "Save",
    "add.saveMore": "Save and add another",
    "add.cancel": "Cancel",
    "add.delete": "Delete",
    "add.deleteSure": "Delete this item?",
    "add.saved": "Saved.",
    "add.deleted": "Deleted.",
    "add.needName": "Give it a name.",
    "add.needPrice": "Give it a price.",

    "scan.button": "Scan barcode",
    "scan.stop": "Stop",
    "scan.looking": "Point at the barcode\u2026",
    "scan.known": "Found: {name}",
    "scan.new": "New barcode. Type the name once and every shop gets it.",
    "scan.none": "Scanning needs the phone app. Type the name here instead.",
    "scan.denied": "Camera not allowed. Type the name instead.",

    "edit.button": "Edit",
    "lang.label": "भाषा / Language",
  },

  hi: {
    "nav.browse": "देखें",
    "nav.post": "मांग डालें",
    "nav.requests": "मांगें",
    "nav.offers": "भाव",
    "nav.catalogue": "मेरी दुकान",
    "nav.chats": "संदेश",
    "nav.account": "खाता",
    "nav.signin": "लॉगिन",

    "cat.title": "मेरी दुकान",
    "cat.sub": "जो आप बेचते हैं वह जोड़ें। आसपास के छात्र उसे खोज लेंगे।",
    "cat.add": "सामान जोड़ें",
    "cat.empty.title": "अभी कुछ नहीं है।",
    "cat.empty.body": "पहला सामान जोड़ें। बस नाम और दाम चाहिए।",
    "cat.count.one": "1 सामान",
    "cat.count.many": "{n} सामान",

    "add.title": "सामान जोड़ें",
    "add.edit": "सामान बदलें",
    "add.photo": "फ़ोटो लगाएं",
    "add.retake": "फ़ोटो बदलें",
    "add.name": "क्या है?",
    "add.name.ph": "ESP8266 बोर्ड",
    "add.price": "दाम",
    "add.category": "किस तरह का",
    "add.more": "और जानकारी",
    "add.specs": "जानकारी",
    "add.stock": "कितने हैं",
    "add.save": "सहेजें",
    "add.saveMore": "सहेजें और दूसरा जोड़ें",
    "add.cancel": "रहने दें",
    "add.delete": "हटाएं",
    "add.deleteSure": "यह सामान हटा दें?",
    "add.saved": "सहेज लिया।",
    "add.deleted": "हटा दिया।",
    "add.needName": "नाम लिखें।",
    "add.needPrice": "दाम लिखें।",

    "scan.button": "बारकोड स्कैन करें",
    "scan.stop": "रोकें",
    "scan.looking": "बारकोड पर कैमरा रखें…",
    "scan.known": "मिल गया: {name}",
    "scan.new": "नया बारकोड। एक बार नाम लिखें, सब दुकानों को मिल जाएगा।",
    "scan.none": "स्कैन के लिए फ़ोन ऐप चाहिए। यहाँ नाम लिख दें।",
    "scan.denied": "कैमरा की अनुमति नहीं है। नाम लिख दें।",

    "edit.button": "बदलें",
    "lang.label": "भाषा / Language",
  },
};

/* Shown on the category tiles, where recognising the picture matters more
   than reading either language. */
window.CATEGORY_HI = {
  Electronics: "इलेक्ट्रॉनिक्स",
  Stationery: "स्टेशनरी",
  "Lab Components": "लैब सामान",
  Books: "किताबें",
  "Computer Parts": "कंप्यूटर पुर्ज़े",
  Tools: "औज़ार",
  Other: "अन्य",
};
