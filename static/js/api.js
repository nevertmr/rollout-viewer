"use strict";
/* 서버 API 호출 */
async function getJSON(url, signal){
  const r = await fetch(url, {signal});
  if(!r.ok) throw new Error(url+" → "+r.status);
  return r.json();
}
