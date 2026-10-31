/* Written for the tests in the shape of the product list that
   tariefkaarten.min.js carried in October 2026: one object per product, its
   code, language and region first, an end month once it is no longer sold,
   then a map of the files of past months. Group buys are listed apart, by
   partner. */
var EVTK = {basePath: "/sites/default/files/inline-files/", products: [
{code:"GS1800V",lang:"nl",name:"1.800 kWh vast",region:"vlaanderen",energy:"elektriciteit",legacy:{"2026-05":[{f:"_0"},{f:"EV-0526-GS1800V-nl-0805_3.pdf",label:{nl:"versie 8 mei"}}],"2026-09":""}},
{code:"GSVI3",lang:"nl",name:"Vaste injectieprijs",region:"vlaanderen",energy:"elektriciteit",legacy:{"2026-09":""}},
{code:"GS3JV",lang:"nl",name:"3 jaar vast",region:"vlaanderen",energy:"elektriciteit",legacy:{"2026-07":"_0"}},
{code:"GSDYN",lang:"nl",name:"Dynamisch",region:"vlaanderen",energy:"elektriciteit",legacy:{"2026-07":"_0"}},
{code:"GSEZ",lang:"nl",name:"Eigen zon",region:"vlaanderen",energy:"elektriciteit",legacy:{"2026-06":"_0"}},
{code:"GSLP",lang:"nl",name:"Laadpunt",region:"vlaanderen",energy:"elektriciteit",legacy:{"2026-06":"_0"}},
{code:"GSEZLP",lang:"nl",name:"Eigen zon + laadpunt",region:"vlaanderen",energy:"elektriciteit",legacy:{"2026-05":"_0"}},
{code:"GSG",lang:"nl",name:"Fossiel gas",note:"variabel",region:"vlaanderen",energy:"gas",legacy:{"2026-05":"_0"}},
{code:"GS1JVG",lang:"nl",name:"1 jaar vast gas",note:"vast",region:"vlaanderen",energy:"gas",from:"2026-08",legacy:{"2026-05":"_0"}},
{code:"GS1800V-WAL",lang:"fr",name:"1.800 kWh fixe",region:"wallonie",energy:"elektriciteit",legacy:{"2026-08":"_0"}},
{code:"GS1JV-WAL",lang:"fr",name:"1 an fixe",region:"wallonie",energy:"elektriciteit",legacy:{"2026-06":"_0"}},
{code:"GSG-WAL",lang:"fr",name:"Gaz fossile",region:"wallonie",energy:"gas",legacy:{"2026-08":"_0"}},
{code:"GS1JVG-WAL",lang:"fr",name:"1 an fixe gaz",region:"wallonie",energy:"gas",from:"2026-08",legacy:{"2026-06":"_0"}},
{code:"GGS1800V-PRO",lang:"nl",name:"1.800 kWh vast PRO",region:"zakelijk",energy:"elektriciteit",legacy:{"2026-08":"_0"}},
{code:"GS1000V",lang:"nl",name:"1.000 kWh vast",region:"vlaanderen",energy:"elektriciteit",end:"2026-02",legacy:{"2026-02":""}},
{code:"ZEE",lang:"nl",name:"Stroom van 't Zeetje",energy:"elektriciteit",region:"vlaanderen",end:"2026-01",legacy:{"2026-01":"/sites/default/files/2025-12/EV-0126-ZEE%20PDF.pdf"}}
], groepsaankoop: [
{partner:"iChoosr",month:"2026-05",type:"vast",files:[{f:"2026-05/EV-0526-GRSO-nl.pdf",r:"vlaanderen",m:"2026-05"}]}
]};
