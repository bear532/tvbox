<?php
$vid=$_GET['id'];//https://raw.githubusercontent.com/Bear9501/news/YTlive/55.m3u8
$nid=$_GET['nid'];
$url="http://888.iptv543.com/smt.php/".$vid.$nid."";
header('location:'.urldecode($url));//,http://richman.cf/tvbox/raw.php?id=51.m3u8
?>
j